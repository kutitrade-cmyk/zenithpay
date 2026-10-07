"""Administrator balance adjustments are atomic and auditable."""
from tests.conftest import open_account


def configured_admin(tmp_path):
    from zenithpay import create_app

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": str(tmp_path / "admin-adjustment.sqlite3"),
            "ADMIN_EMAIL": "root@zenith.test",
            "ADMIN_PASSWORD": "LongAdminPassword123!",
        }
    )
    admin = app.test_client()
    assert admin.post(
        "/api/auth/login",
        json={"email": "root@zenith.test", "password": "LongAdminPassword123!"},
    ).status_code == 200
    member = app.test_client()
    assert member.post(
        "/api/auth/register",
        json={"name": "Account owner", "email": "owner@zenith.test", "password": "password123"},
    ).status_code == 201
    return app, admin, member


def test_admin_adjustment_changes_balance_and_writes_receipted_audit(tmp_path):
    app, admin, member = configured_admin(tmp_path)
    account = open_account(member, "checking", "US", "Everyday")

    added = admin.post(
        "/api/admin/adjustments",
        json={
            "account_id": account,
            "direction": "credit",
            "amount_cents": 25_000,
            "reason": "Verified opening correction",
        },
    )
    assert added.status_code == 201
    transaction = added.get_json()["transaction"]
    assert transaction["receipt_number"]
    assert member.get(f"/api/accounts/{account}").get_json()["account"]["balance_cents"] == 25_000
    receipt = admin.get(
        f"/api/transactions/{transaction['id']}/receipt"
    ).get_json()["receipt"]
    assert receipt["receipt_details"]["admin_email"] == "root@zenith.test"

    removed = admin.post(
        "/api/admin/adjustments",
        json={
            "account_id": account,
            "direction": "debit",
            "amount_cents": 5_000,
            "reason": "Duplicate deposit reversal",
        },
    )
    assert removed.status_code == 201
    assert member.get(f"/api/accounts/{account}").get_json()["account"]["balance_cents"] == 20_000
    listing = admin.get("/api/admin/overview").get_json()
    assert len(listing["adjustments"]) == 2
    with app.app_context():
        from zenithpay.db import get_db

        audit = get_db().execute("SELECT * FROM admin_audit_log ORDER BY id").fetchall()
    assert len(audit) == 2
    assert audit[0]["reason"] == "Verified opening correction"
    assert audit[1]["direction"] == "debit"


def test_admin_adjustment_rejects_insufficient_funds_and_non_admin(tmp_path):
    _app, admin, member = configured_admin(tmp_path)
    account = open_account(member, "checking", "US", "Everyday")
    payload = {
        "account_id": account,
        "direction": "debit",
        "amount_cents": 1,
        "reason": "Attempted unsupported debit",
    }
    denied = member.post("/api/admin/adjustments", json=payload)
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "admin_required"

    insufficient = admin.post("/api/admin/adjustments", json=payload)
    assert insufficient.status_code == 400
    assert insufficient.get_json()["code"] == "insufficient_funds"
    assert member.get(f"/api/accounts/{account}").get_json()["account"]["balance_cents"] == 0


def test_transaction_receipt_is_owner_scoped(auth_client, client):
    from tests.conftest import open_account

    account = open_account(auth_client, "checking", "US", "Receipt account")
    transaction = auth_client.post(
        f"/api/accounts/{account}/transactions",
        json={"direction": "credit", "amount_cents": 12_345, "description": "Receipt test"},
    ).get_json()["transaction"]
    receipt = auth_client.get(f"/api/transactions/{transaction['id']}/receipt")
    assert receipt.status_code == 200
    body = receipt.get_json()["receipt"]
    assert body["receipt_number"] == transaction["receipt_number"]
    assert body["account"]["account_number_last4"]

    client.post(
        "/api/auth/register",
        json={"name": "Stranger", "email": "stranger@zenith.test", "password": "password123"},
    )
    denied = client.get(f"/api/transactions/{transaction['id']}/receipt")
    assert denied.status_code == 404
    assert denied.get_json()["code"] == "transaction_not_found"
