"""Auth: register/login/logout/me, validation, password hashing."""
from werkzeug.security import check_password_hash

from zenithpay.db import get_db


def test_register_creates_session_and_returns_user(client):
    resp = client.post(
        "/api/auth/register",
        json={"name": "Ada Member", "email": "ada@zenith.test", "password": "password123"},
    )
    assert resp.status_code == 201
    user = resp.get_json()["user"]
    assert user["email"] == "ada@zenith.test"
    assert "password" not in user

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.get_json()["user"]["id"] == user["id"]


def test_duplicate_email_rejected(client):
    payload = {"name": "Ada", "email": "ada@zenith.test", "password": "password123"}
    assert client.post("/api/auth/register", json=payload).status_code == 201
    again = client.post("/api/auth/register", json=payload)
    assert again.status_code == 409
    assert again.get_json()["code"] == "email_taken"


def test_register_validation(client):
    base = {"name": "Ada", "email": "ada@zenith.test", "password": "password123"}

    weak = dict(base, password="short")
    assert client.post("/api/auth/register", json=weak).status_code == 400

    bad_email = dict(base, email="not-an-email")
    resp = client.post("/api/auth/register", json=bad_email)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_email"

    no_name = dict(base, name="   ")
    assert client.post("/api/auth/register", json=no_name).status_code == 400

    # duplicate check must not fire before validation
    assert client.post("/api/auth/register", json={}).status_code == 400


def test_register_pin_validation_and_status(client, app):
    base = {
        "name": "Ada",
        "email": "ada-pin@zenith.test",
        "password": "password123",
    }
    invalid = dict(base, pin="12345", confirm_pin="12345")
    response = client.post("/api/auth/register", json=invalid)
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_pin"

    registered = client.post(
        "/api/auth/register",
        json=dict(base, pin="987654", confirm_pin="987654"),
    )
    assert registered.status_code == 201
    assert registered.get_json()["user"]["has_pin"] is True
    with app.app_context():
        row = get_db().execute(
            "SELECT pin_hash FROM users WHERE email = ?", ("ada-pin@zenith.test",)
        ).fetchone()
    assert row["pin_hash"] != "987654"
    assert "987654" not in row["pin_hash"]


def test_set_pin_validates_confirmation_and_cannot_be_replaced(auth_client):
    mismatch = auth_client.post(
        "/api/auth/pin", json={"pin": "123456", "confirm_pin": "654321"}
    )
    assert mismatch.status_code == 400
    assert mismatch.get_json()["code"] == "invalid_pin"

    already_set = auth_client.post(
        "/api/auth/pin", json={"pin": "123456", "confirm_pin": "123456"}
    )
    assert already_set.status_code == 409
    assert already_set.get_json()["code"] == "pin_already_set"


def test_login_logout_flow(client):
    client.post(
        "/api/auth/register",
        json={"name": "Ada", "email": "ada@zenith.test", "password": "password123"},
    )
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401

    wrong = client.post(
        "/api/auth/login", json={"email": "ada@zenith.test", "password": "wrongpass1"}
    )
    assert wrong.status_code == 401
    assert wrong.get_json()["code"] == "invalid_credentials"

    unknown = client.post(
        "/api/auth/login", json={"email": "nobody@zenith.test", "password": "password123"}
    )
    assert unknown.status_code == 401  # same message as wrong password

    ok = client.post(
        "/api/auth/login", json={"email": "ada@zenith.test", "password": "password123"}
    )
    assert ok.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_login_email_is_case_insensitive(client):
    client.post(
        "/api/auth/register",
        json={"name": "Ada", "email": "ada@zenith.test", "password": "password123"},
    )
    resp = client.post(
        "/api/auth/login", json={"email": "ADA@ZENITH.TEST", "password": "password123"}
    )
    assert resp.status_code == 200


def test_password_is_hashed_not_plaintext(app, client):
    client.post(
        "/api/auth/register",
        json={"name": "Ada", "email": "ada@zenith.test", "password": "password123"},
    )
    with app.app_context():
        row = get_db().execute(
            "SELECT password_hash FROM users WHERE email = ?", ("ada@zenith.test",)
        ).fetchone()
    assert row["password_hash"] != "password123"
    assert "password123" not in row["password_hash"]
    assert check_password_hash(row["password_hash"], "password123")


def test_protected_routes_require_auth(client):
    for method, url in (
        ("get", "/api/accounts"),
        ("post", "/api/accounts"),
        ("post", "/api/auth/pin"),
        ("get", "/api/accounts/1/transactions"),
        ("post", "/api/accounts/1/transactions"),
    ):
        resp = getattr(client, method)(url, json={})
        assert resp.status_code == 401, url
        assert resp.get_json()["code"] == "auth_required"


def test_profile_update_stores_valid_photo_and_rejects_invalid_image(auth_client):
    import base64

    png = base64.b64encode(b"\x89PNG\r\n\x1a\nvalid-test-image").decode("ascii")
    response = auth_client.patch(
        "/api/auth/profile",
        json={"name": "Updated Member", "avatar_data": f"data:image/png;base64,{png}"},
    )
    assert response.status_code == 200
    assert response.get_json()["user"]["name"] == "Updated Member"
    assert response.get_json()["user"]["avatar_data"].startswith("data:image/png;base64,")

    invalid = auth_client.patch(
        "/api/auth/profile",
        json={"name": "Updated Member", "avatar_data": "data:image/png;base64,SGVsbG8="},
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["code"] == "invalid_avatar"


def test_bootstrap_admin_and_member_are_authorized_separately(tmp_path):
    from zenithpay import create_app

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": str(tmp_path / "admin.sqlite3"),
            "ADMIN_EMAIL": "root@zenith.test",
            "ADMIN_PASSWORD": "LongAdminPassword123!",
        }
    )
    admin_client = app.test_client()
    login = admin_client.post(
        "/api/auth/login",
        json={"email": "root@zenith.test", "password": "LongAdminPassword123!"},
    )
    assert login.status_code == 200
    assert login.get_json()["user"]["role"] == "admin"
    assert admin_client.get("/api/admin/overview").status_code == 200
    assert admin_client.get("/admin").status_code == 200

    member_client = app.test_client()
    member_client.post(
        "/api/auth/register",
        json={"name": "Normal member", "email": "normal@zenith.test", "password": "password123"},
    )
    denied = member_client.get("/api/admin/overview")
    assert denied.status_code == 403
    assert denied.get_json()["code"] == "admin_required"
    assert member_client.get("/admin").headers["Location"] == "/dashboard"


def test_upgrade_existing_users_table_adds_profile_and_role_columns(tmp_path):
    import sqlite3

    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT 'now'
        )
        """
    )
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()

    from zenithpay import create_app

    app = create_app(
        {"TESTING": True, "SECRET_KEY": "test", "DATABASE": str(database)}
    )
    with app.app_context():
        columns = {
            row["name"]
            for row in get_db().execute("PRAGMA table_info(users)").fetchall()
        }
        transaction_columns = {
            row["name"]
            for row in get_db().execute("PRAGMA table_info(transactions)").fetchall()
        }
    assert {"role", "avatar_data", "pin_hash"} <= columns
    assert "receipt_details" in transaction_columns
