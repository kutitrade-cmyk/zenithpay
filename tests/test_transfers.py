"""Money movement: internal transfers, P2P payments, loan repayments."""
from tests.conftest import open_account
from zenithpay.db import get_db


def fund(client, account_id, amount=1_000_000, description="Funding"):
    resp = client.post(
        f"/api/accounts/{account_id}/transactions",
        json={"direction": "credit", "amount_cents": amount, "description": description},
    )
    assert resp.status_code == 201, resp.get_json()
    return resp


def balance(client, account_id):
    return client.get(f"/api/accounts/{account_id}").get_json()["account"]["balance_cents"]


def transfer(client, source, dest, amount, description="Move"):
    return client.post(
        "/api/transfers",
        json={
            "source_account_id": source,
            "dest_account_id": dest,
            "amount_cents": amount,
            "description": description,
        },
    )


def p2p(client, source, recipient, amount, description="Send", **extra):
    return client.post(
        "/api/p2p",
        json={
            "source_account_id": source,
            "recipient": recipient,
            "amount_cents": amount,
            "description": description,
            **extra,
        },
    )


def test_internal_transfer_moves_atomic_pair(auth_client):
    src = open_account(auth_client, "checking", "US", "Checking")
    dst = open_account(auth_client, "savings", "US", "Savings")
    fund(auth_client, src, 100_000)

    resp = transfer(auth_client, src, dst, 25_000, "Move to savings")
    assert resp.status_code == 201
    body = resp.get_json()
    group = body["transfer_group_id"]
    assert group
    assert body["source_balance_cents"] == 75_000

    entries = body["transactions"]
    assert len(entries) == 2
    assert entries[0]["direction"] == "debit"
    assert entries[0]["balance_after_cents"] == 75_000
    assert entries[1]["direction"] == "credit"
    assert entries[1]["balance_after_cents"] == 25_000
    assert entries[0]["transfer_group_id"] == entries[1]["transfer_group_id"] == group

    assert balance(auth_client, src) == 75_000
    assert balance(auth_client, dst) == 25_000


def test_transfer_insufficient_funds_writes_nothing(auth_client):
    src = open_account(auth_client, "checking", "US", "Checking")
    dst = open_account(auth_client, "savings", "US", "Savings")
    fund(auth_client, src, 10_000)

    resp = transfer(auth_client, src, dst, 20_000)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "insufficient_funds"

    assert balance(auth_client, src) == 10_000
    assert balance(auth_client, dst) == 0
    listing = auth_client.get(f"/api/accounts/{dst}/transactions").get_json()
    assert listing["total"] == 0  # no orphan credit landed


def test_transfer_rejects_self_and_bad_amounts(auth_client):
    src = open_account(auth_client, "checking", "US", "Checking")
    assert transfer(auth_client, src, src, 100).get_json()["code"] == "self_transfer"
    assert transfer(auth_client, src, src, 0).get_json()["code"] == "invalid_amount"
    bad = auth_client.post(
        "/api/transfers",
        json={"source_account_id": src, "amount_cents": 100, "description": "x"},
    )
    assert bad.get_json()["code"] == "invalid_dest_account_id"


def test_transfer_requires_same_currency(auth_client):
    us = open_account(auth_client, "checking", "US", "Checking")
    gb = open_account(auth_client, "savings", "GB", "Pound Savings")
    resp = transfer(auth_client, us, gb, 1_000)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "currency_mismatch"


def test_transfer_excludes_loan_accounts(auth_client):
    src = open_account(auth_client, "checking", "US", "Checking")
    loan = open_account(auth_client, "loan", "US", "Car Loan")
    assert transfer(auth_client, src, loan, 100).get_json()["code"] == "invalid_account_type"
    assert transfer(auth_client, loan, src, 100).get_json()["code"] == "invalid_account_type"


def test_transfer_frozen_account_blocked(app, auth_client):
    src = open_account(auth_client, "checking", "US", "Checking")
    dst = open_account(auth_client, "savings", "US", "Savings")
    with app.app_context():
        db = get_db()
        db.execute("UPDATE accounts SET status = 'frozen' WHERE id = ?", (src,))
        db.commit()
    resp = transfer(auth_client, src, dst, 100)
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "account_inactive"


def test_transfer_hides_foreign_accounts(auth_client, client):
    src = open_account(auth_client, "checking", "US", "Checking")
    client.post(
        "/api/auth/register",
        json={"name": "Bob Member", "email": "bob@zenith.test", "password": "password123"},
    )
    bob_account = open_account(client, "checking", "US", "Bob Checking")

    # Bob cannot move Ada's money
    assert transfer(client, src, bob_account, 100).status_code == 404
    # Ada cannot target Bob's account either
    assert transfer(auth_client, src, bob_account, 100).status_code == 404


def test_p2p_by_account_number(auth_client, client):
    client.post(
        "/api/auth/register",
        json={"name": "Bob Member", "email": "bob@zenith.test", "password": "password123"},
    )
    bob_account = open_account(client, "checking", "US", "Bob Checking")
    ada_account = open_account(auth_client, "checking", "US", "Ada Checking")
    fund(auth_client, ada_account, 50_000)
    account_number = client.get(f"/api/accounts/{bob_account}").get_json()["account"]["account_number"]

    resp = p2p(auth_client, ada_account, account_number, 7_500, "Lunch split")
    assert resp.status_code == 201
    body = resp.get_json()
    entries = body["transactions"]
    assert len(entries) == 2
    assert entries[0]["account_id"] == ada_account
    assert entries[0]["counterparty"] == "Bob Member"
    assert entries[1]["account_id"] == bob_account
    assert entries[1]["direction"] == "credit"
    assert entries[1]["counterparty"] == "Ada Member"
    assert entries[0]["transfer_group_id"] == entries[1]["transfer_group_id"]

    assert balance(auth_client, ada_account) == 42_500
    assert balance(client, bob_account) == 7_500


def test_p2p_by_email_prefers_checking(auth_client, client):
    client.post(
        "/api/auth/register",
        json={"name": "Bob Member", "email": "bob@zenith.test", "password": "password123"},
    )
    bob_savings = open_account(client, "savings", "US", "Bob Savings")  # created first
    bob_checking = open_account(client, "checking", "US", "Bob Checking")
    ada_account = open_account(auth_client, "checking", "US", "Ada Checking")
    fund(auth_client, ada_account, 10_000)

    resp = p2p(auth_client, ada_account, "BOB@ZENITH.TEST", 2_000, "Dinner")
    assert resp.status_code == 201
    assert balance(client, bob_checking) == 2_000
    assert balance(client, bob_savings) == 0


def test_p2p_rejects_self_and_unknown(auth_client):
    account = open_account(auth_client, "checking", "US", "Checking")
    fund(auth_client, account, 5_000)

    own = p2p(auth_client, account, "ada@zenith.test", 100)
    assert own.get_json()["code"] == "self_transfer"

    unknown_email = p2p(auth_client, account, "ghost@zenith.test", 100)
    assert unknown_email.status_code == 404
    assert unknown_email.get_json()["code"] == "recipient_not_found"

    unknown_number = p2p(auth_client, account, "9999999999", 100)
    assert unknown_number.status_code == 404

    empty = p2p(auth_client, account, "  ", 100)
    assert empty.get_json()["code"] == "invalid_recipient"


def test_p2p_is_atomic_across_users(auth_client, client):
    client.post(
        "/api/auth/register",
        json={"name": "Bob Member", "email": "bob@zenith.test", "password": "password123"},
    )
    bob_account = open_account(client, "checking", "US", "Bob Checking")
    ada_account = open_account(auth_client, "checking", "US", "Ada Checking")
    fund(auth_client, ada_account, 1_000)

    resp = p2p(auth_client, ada_account, "bob@zenith.test", 5_000, "Too much")
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "insufficient_funds"

    assert balance(auth_client, ada_account) == 1_000
    assert balance(client, bob_account) == 0  # nothing partial landed


def test_p2p_currency_mismatch(auth_client, client):
    client.post(
        "/api/auth/register",
        json={"name": "Bob Member", "email": "bob@zenith.test", "password": "password123"},
    )
    bob_account = open_account(client, "savings", "GB", "Bob Pounds")
    ada_account = open_account(auth_client, "checking", "US", "Ada Checking")
    fund(auth_client, ada_account, 10_000)

    resp = p2p(auth_client, ada_account, "bob@zenith.test", 100)
    assert resp.get_json()["code"] == "currency_mismatch"


def test_repayment_shrinks_loan(auth_client):
    checking = open_account(auth_client, "checking", "US", "Checking")
    loan = open_account(auth_client, "loan", "US", "Appliance Loan")
    fund(auth_client, checking, 100_000)
    fund(auth_client, loan, 500_000, description="Drawdown")  # credit = debt up

    resp = auth_client.post(
        "/api/repayments",
        json={
            "source_account_id": checking,
            "loan_account_id": loan,
            "amount_cents": 20_000,
            "description": "February repayment",
        },
    )
    assert resp.status_code == 201
    entries = resp.get_json()["transactions"]
    assert len(entries) == 2
    assert all(entry["direction"] == "debit" for entry in entries)
    assert entries[0]["transfer_group_id"] == entries[1]["transfer_group_id"]
    assert entries[1]["category"] == "repayment"

    assert balance(auth_client, checking) == 80_000
    assert balance(auth_client, loan) == 480_000  # owed shrank


def test_repayment_validation(auth_client):
    checking = open_account(auth_client, "checking", "US", "Checking")
    loan = open_account(auth_client, "loan", "US", "Loan")
    gb_loan = open_account(auth_client, "loan", "GB", "Pound Loan")
    savings = open_account(auth_client, "savings", "US", "Savings")
    fund(auth_client, checking, 10_000)

    wrong_type = auth_client.post(
        "/api/repayments",
        json={"source_account_id": checking, "loan_account_id": savings, "amount_cents": 100},
    )
    assert wrong_type.get_json()["code"] == "invalid_account_type"

    mismatch = auth_client.post(
        "/api/repayments",
        json={"source_account_id": checking, "loan_account_id": gb_loan, "amount_cents": 100},
    )
    assert mismatch.get_json()["code"] == "currency_mismatch"

    broke = auth_client.post(
        "/api/repayments",
        json={"source_account_id": checking, "loan_account_id": loan, "amount_cents": 999_999},
    )
    assert broke.get_json()["code"] == "insufficient_funds"
