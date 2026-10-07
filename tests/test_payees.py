"""Payees: CRUD + Bill Pay now."""
from tests.conftest import open_account


def fund(client, account_id, amount=1_000_000):
    resp = client.post(
        f"/api/accounts/{account_id}/transactions",
        json={"direction": "credit", "amount_cents": amount, "description": "Funding"},
    )
    assert resp.status_code == 201


def make_payee(client, name="PowerGrid Electric", **extra):
    resp = client.post(
        "/api/payees",
        json={"name": name, "category": "utilities", "reference_number": "PG-4417-88", **extra},
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["payee"]


def test_payee_crud_roundtrip(auth_client):
    payee = make_payee(auth_client)
    assert payee["name"] == "PowerGrid Electric"
    assert payee["category"] == "utilities"
    assert payee["reference_number"] == "PG-4417-88"

    listing = auth_client.get("/api/payees").get_json()["payees"]
    assert len(listing) == 1

    patched = auth_client.patch(
        f"/api/payees/{payee['id']}", json={"category": "energy", "notes": "Account 12B"}
    ).get_json()["payee"]
    assert patched["category"] == "energy"
    assert patched["notes"] == "Account 12B"
    assert patched["name"] == "PowerGrid Electric"  # untouched

    assert auth_client.delete(f"/api/payees/{payee['id']}").status_code == 204
    assert auth_client.get("/api/payees").get_json()["payees"] == []


def test_payee_validation(auth_client):
    make_payee(auth_client)

    empty = auth_client.post("/api/payees", json={"name": "   "})
    assert empty.status_code == 400
    assert empty.get_json()["code"] == "invalid_name"

    dup = auth_client.post("/api/payees", json={"name": "PowerGrid Electric"})
    assert dup.status_code == 409
    assert dup.get_json()["code"] == "duplicate_payee"

    existing = auth_client.get("/api/payees").get_json()["payees"][0]
    make_payee(auth_client, name="Fiberline Internet")
    dup_patch = auth_client.patch(
        f"/api/payees/{existing['id']}", json={"name": "Fiberline Internet"}
    )
    assert dup_patch.status_code == 409
    assert dup_patch.get_json()["code"] == "duplicate_payee"

    # renaming to its own name is a harmless no-op
    same = auth_client.patch(
        f"/api/payees/{existing['id']}", json={"name": "PowerGrid Electric"}
    )
    assert same.status_code == 200

    no_fields = auth_client.patch(f"/api/payees/{existing['id']}", json={})
    assert no_fields.get_json()["code"] == "no_updates"


def test_payees_scoped_to_owner(auth_client, client):
    client.post(
        "/api/auth/register",
        json={"name": "Bob Member", "email": "bob@zenith.test", "password": "password123"},
    )
    bob_payee = make_payee(client, name="Bob's ISP")

    assert auth_client.get("/api/payees").get_json()["payees"] == []
    assert auth_client.patch(f"/api/payees/{bob_payee['id']}", json={"name": "Hacked"}).status_code == 404
    assert auth_client.delete(f"/api/payees/{bob_payee['id']}").status_code == 404

    account = open_account(auth_client, "checking", "US", "Checking")
    fund(auth_client, account)
    resp = auth_client.post(
        f"/api/payees/{bob_payee['id']}/pay",
        json={"source_account_id": account, "amount_cents": 100},
    )
    assert resp.status_code == 404


def test_bill_pay_now(auth_client):
    account = open_account(auth_client, "checking", "US", "Checking")
    fund(auth_client, account, 50_000)
    payee = make_payee(auth_client)

    resp = auth_client.post(
        f"/api/payees/{payee['id']}/pay",
        json={"source_account_id": account, "amount_cents": 8_999},
    )
    assert resp.status_code == 201
    body = resp.get_json()
    txn = body["transaction"]
    assert txn["direction"] == "debit"
    assert txn["counterparty"] == "PowerGrid Electric"
    assert txn["category"] == "utilities"  # inherited from the payee
    assert txn["balance_after_cents"] == 41_001
    assert body["account_balance_cents"] == 41_001

    broke = auth_client.post(
        f"/api/payees/{payee['id']}/pay",
        json={"source_account_id": account, "amount_cents": 999_999},
    )
    assert broke.get_json()["code"] == "insufficient_funds"


def test_bill_pay_missing_payee(auth_client):
    account = open_account(auth_client, "checking", "US", "Checking")
    fund(auth_client, account)
    resp = auth_client.post(
        "/api/payees/99999/pay",
        json={"source_account_id": account, "amount_cents": 100},
    )
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "payee_not_found"


def test_delete_payee_blocked_while_scheduled(auth_client):
    account = open_account(auth_client, "checking", "US", "Checking")
    fund(auth_client, account)
    payee = make_payee(auth_client)

    scheduled = auth_client.post(
        "/api/scheduled-payments",
        json={
            "kind": "bill_pay",
            "source_account_id": account,
            "payee_id": payee["id"],
            "amount_cents": 8_999,
            "description": "Electric bill",
            "frequency": "monthly",
            "next_due_at": "2099-01-01",
        },
    )
    assert scheduled.status_code == 201
    schedule_id = scheduled.get_json()["scheduled_payment"]["id"]

    blocked = auth_client.delete(f"/api/payees/{payee['id']}")
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "payee_in_use"

    auth_client.patch(f"/api/scheduled-payments/{schedule_id}", json={"status": "cancelled"})
    assert auth_client.delete(f"/api/payees/{payee['id']}").status_code == 204
