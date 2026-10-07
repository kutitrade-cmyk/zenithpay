"""Transactions: ledger math, filters, search, pagination, ownership."""
from tests.conftest import open_account


def record(client, account_id, direction, amount, description, **extra):
    payload = {
        "direction": direction,
        "amount_cents": amount,
        "description": description,
        **extra,
    }
    return client.post(f"/api/accounts/{account_id}/transactions", json=payload)


def test_record_credit_and_debit_updates_balance(auth_client):
    aid = open_account(auth_client)

    first = record(auth_client, aid, "credit", 100_000, "Paycheck")
    assert first.status_code == 201
    body = first.get_json()
    assert body["account_balance_cents"] == 100_000
    assert body["transaction"]["balance_after_cents"] == 100_000
    assert body["transaction"]["direction"] == "credit"

    second = record(auth_client, aid, "debit", 2_500, "Coffee")
    assert second.status_code == 201
    assert second.get_json()["account_balance_cents"] == 97_500
    assert second.get_json()["transaction"]["balance_after_cents"] == 97_500

    listing = auth_client.get(f"/api/accounts/{aid}/transactions").get_json()
    assert listing["total"] == 2
    assert listing["account_balance_cents"] == 97_500
    # newest first, balances chain down the statement
    assert [t["direction"] for t in listing["transactions"]] == ["debit", "credit"]
    assert listing["transactions"][0]["balance_after_cents"] == 97_500
    assert listing["transactions"][1]["balance_after_cents"] == 100_000


def test_insufficient_funds_blocks_debit(auth_client):
    aid = open_account(auth_client)
    record(auth_client, aid, "credit", 10_000, "Deposit")

    resp = record(auth_client, aid, "debit", 20_000, "Too much")
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "insufficient_funds"

    listing = auth_client.get(f"/api/accounts/{aid}/transactions").get_json()
    assert listing["total"] == 1  # nothing partial was written
    assert listing["account_balance_cents"] == 10_000


def test_exact_balance_debit_allowed(auth_client):
    aid = open_account(auth_client)
    record(auth_client, aid, "credit", 5_000, "Deposit")
    resp = record(auth_client, aid, "debit", 5_000, "Empty it")
    assert resp.status_code == 201
    assert resp.get_json()["account_balance_cents"] == 0


def test_loan_overpayment_allowed(auth_client):
    aid = open_account(auth_client, "loan", "DE", "Community Loan")
    record(auth_client, aid, "credit", 500_000, "Loan disbursement")
    resp = record(auth_client, aid, "debit", 600_000, "Overpay")
    assert resp.status_code == 201
    assert resp.get_json()["account_balance_cents"] == -100_000


def test_amount_and_field_validation(auth_client):
    aid = open_account(auth_client)

    for bad_amount in (0, -5, "100", 1.5, True, None):
        resp = record(auth_client, aid, "credit", bad_amount, "X")
        assert resp.status_code == 400, bad_amount
        assert resp.get_json()["code"] == "invalid_amount"

    assert record(auth_client, aid, "credit", 100, "").status_code == 400

    no_direction = auth_client.post(
        f"/api/accounts/{aid}/transactions",
        json={"amount_cents": 100, "description": "X"},
    )
    assert no_direction.status_code == 400
    assert no_direction.get_json()["code"] == "invalid_direction"

    too_large = record(auth_client, aid, "credit", 10_000_000_001, "X")
    assert too_large.status_code == 400
    assert too_large.get_json()["code"] == "amount_too_large"


def test_search_matches_description_counterparty_category(auth_client):
    aid = open_account(auth_client)
    record(auth_client, aid, "credit", 250_000, "Salary",
           category="income", counterparty="Acme Cooperative")
    record(auth_client, aid, "debit", 4_299, "Weekly groceries",
           category="groceries", counterparty="Fresh Market")
    record(auth_client, aid, "debit", 2_450, "Bus fare", category="transport")

    base = f"/api/accounts/{aid}/transactions"

    assert auth_client.get(f"{base}?search=grocer").get_json()["total"] == 1
    assert auth_client.get(f"{base}?search=acme").get_json()["total"] == 1  # counterparty
    assert auth_client.get(f"{base}?search=MARKET").get_json()["total"] == 1  # case-insens.
    assert auth_client.get(f"{base}?search=zzz").get_json()["total"] == 0


def test_direction_category_and_amount_filters(auth_client):
    aid = open_account(auth_client)
    record(auth_client, aid, "credit", 1_000_000, "Opening funds", category="deposit")
    record(auth_client, aid, "debit", 2_000, "Mid spend", category="food")
    record(auth_client, aid, "debit", 30_000, "Big spend", category="housing")

    base = f"/api/accounts/{aid}/transactions"

    debits = auth_client.get(f"{base}?direction=debit").get_json()
    assert debits["total"] == 2

    credits = auth_client.get(f"{base}?direction=credit").get_json()
    assert credits["total"] == 1

    food = auth_client.get(f"{base}?category=food").get_json()
    assert food["total"] == 1
    assert food["transactions"][0]["description"] == "Mid spend"

    ranged = auth_client.get(f"{base}?min=1500&max=25000").get_json()
    assert ranged["total"] == 1
    assert ranged["transactions"][0]["amount_cents"] == 2_000


def test_filter_validation(auth_client):
    aid = open_account(auth_client)
    base = f"/api/accounts/{aid}/transactions"

    assert auth_client.get(f"{base}?direction=sideways").status_code == 400
    assert auth_client.get(f"{base}?from=2026-13-45").status_code == 400
    assert auth_client.get(f"{base}?to=not-a-date").status_code == 400
    assert auth_client.get(f"{base}?min=abc").status_code == 400
    assert auth_client.get(f"{base}?min=5000&max=100").status_code == 400
    assert auth_client.get(f"{base}?min=-1").status_code == 400
    assert auth_client.get(
        f"{base}?from=2026-10-05&to=2026-10-01"
    ).status_code == 400
    assert auth_client.get(f"{base}?limit=0").status_code == 400
    assert auth_client.get(f"{base}?limit=999").status_code == 400
    assert auth_client.get(f"{base}?offset=-1").status_code == 400


def test_date_range_filters(auth_client):
    aid = open_account(auth_client)
    record(auth_client, aid, "credit", 1_000, "Today's deposit")

    base = f"/api/accounts/{aid}/transactions"
    today = auth_client.get(f"{base}?from=2026-10-01&to=2026-10-31").get_json()
    assert today["total"] == 1  # seeded entries are "now" (current date)

    empty = auth_client.get(f"{base}?to=2020-01-01").get_json()
    assert empty["total"] == 0

    future = auth_client.get(f"{base}?from=2099-01-01").get_json()
    assert future["total"] == 0


def test_pagination(auth_client):
    aid = open_account(auth_client)
    for i in range(7):
        record(auth_client, aid, "credit", 100, f"Deposit {i}")

    base = f"/api/accounts/{aid}/transactions"
    page1 = auth_client.get(f"{base}?limit=3&offset=0").get_json()
    page2 = auth_client.get(f"{base}?limit=3&offset=3").get_json()
    assert page1["total"] == page2["total"] == 7
    assert len(page1["transactions"]) == 3
    assert len(page2["transactions"]) == 3
    ids1 = {t["id"] for t in page1["transactions"]}
    ids2 = {t["id"] for t in page2["transactions"]}
    assert not ids1 & ids2  # no overlap


def test_transactions_scoped_to_owner(auth_client, client):
    aid = open_account(auth_client)
    record(auth_client, aid, "credit", 5_000, "Private deposit")

    client.post(
        "/api/auth/register",
        json={"name": "Bob", "email": "bob@zenith.test", "password": "password123"},
    )
    # stranger gets 404 for both read and write
    assert client.get(f"/api/accounts/{aid}/transactions").status_code == 404
    stranger_write = record(client, aid, "debit", 100, "Intrusion")
    assert stranger_write.status_code == 404

    # owner unaffected
    listing = auth_client.get(f"/api/accounts/{aid}/transactions").get_json()
    assert listing["total"] == 1
    assert listing["account_balance_cents"] == 5_000
