"""Accounts: dashboard, creation with generated identifiers, details, ownership."""
from tests.conftest import open_account
from zenithpay.db import get_db
from zenithpay.iban import validate_iban


def test_create_us_checking_generates_identifiers(auth_client):
    resp = auth_client.post(
        "/api/accounts",
        json={"type": "checking", "country_code": "US", "nickname": "Everyday"},
    )
    assert resp.status_code == 201
    account = resp.get_json()["account"]
    assert account["type"] == "checking"
    assert account["balance_cents"] == 0
    assert account["status"] == "active"
    assert account["currency"] == "USD"  # default for US
    assert account["iban"] is None  # US is not an IBAN country
    assert len(account["account_number"]) == 12
    assert account["account_number"].isdigit()
    routing = account["routing_number"]
    assert len(routing) == 9 and routing.isdigit()
    d = [int(c) for c in routing]
    checksum = (
        3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])
    )
    assert checksum % 10 == 0  # valid ABA checksum


def test_create_gb_savings_with_valid_iban(auth_client):
    resp = auth_client.post(
        "/api/accounts",
        json={"type": "savings", "country_code": "gb", "nickname": "Rainy Day"},
    )
    assert resp.status_code == 201
    account = resp.get_json()["account"]
    assert account["country_code"] == "GB"  # normalized
    assert account["currency"] == "GBP"
    assert account["iban"].startswith("GB")
    compact = "".join(account["iban"].split())
    assert len(compact) == 22
    assert validate_iban(compact)


def test_create_loan_account(auth_client):
    resp = auth_client.post(
        "/api/accounts",
        json={"type": "loan", "country_code": "DE", "nickname": "Community Loan"},
    )
    assert resp.status_code == 201
    account = resp.get_json()["account"]
    assert account["type"] == "loan"
    assert account["currency"] == "EUR"
    assert validate_iban("".join(account["iban"].split()))


def test_create_account_validation(auth_client):
    base = {"type": "checking", "country_code": "US", "nickname": "Main"}

    bad_type = dict(base, type="crypto")
    resp = auth_client.post("/api/accounts", json=bad_type)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_type"

    bad_country = dict(base, country_code="NG")
    resp = auth_client.post("/api/accounts", json=bad_country)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "unsupported_country"

    no_nickname = dict(base, nickname="  ")
    assert auth_client.post("/api/accounts", json=no_nickname).status_code == 400

    bad_currency = dict(base, currency="DOLLARS")
    resp = auth_client.post("/api/accounts", json=bad_currency)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_currency"

    # unknown/unsupported country must not leave accounts behind
    dashboard = auth_client.get("/api/accounts")
    assert dashboard.get_json()["summary"]["count"] == 0


def test_account_creation_requires_pin(client, app):
    registered = client.post(
        "/api/auth/register",
        json={
            "name": "Pinless Member",
            "email": "pinless@zenith.test",
            "password": "password123",
        },
    )
    assert registered.status_code == 201

    payload = {"type": "checking", "country_code": "US", "nickname": "Everyday"}
    blocked = client.post("/api/accounts", json=payload)
    assert blocked.status_code == 403
    assert blocked.get_json()["code"] == "pin_required"
    assert client.get("/api/accounts").get_json()["summary"]["count"] == 0

    pin = client.post(
        "/api/auth/pin", json={"pin": "123456", "confirm_pin": "123456"}
    )
    assert pin.status_code == 200
    with app.app_context():
        row = get_db().execute(
            "SELECT pin_hash FROM users WHERE email = ?", ("pinless@zenith.test",)
        ).fetchone()
    assert row["pin_hash"] != "123456"
    from werkzeug.security import check_password_hash

    assert check_password_hash(row["pin_hash"], "123456")
    assert client.post("/api/accounts", json=payload).status_code == 201


def test_dashboard_lists_accounts_with_totals(auth_client):
    open_account(auth_client, "checking", "US", "Everyday")
    open_account(auth_client, "savings", "GB", "Rainy Day")

    resp = auth_client.get("/api/accounts")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["summary"]["count"] == 2
    assert body["summary"]["balance_cents_by_currency"] == {"USD": 0, "GBP": 0}
    nicknames = [a["nickname"] for a in body["accounts"]]
    assert nicknames == ["Everyday", "Rainy Day"]


def test_accounts_are_scoped_to_owner(auth_client, client):
    account_id = open_account(auth_client)

    # second member registers and must not see nor reach the account
    client.post(
        "/api/auth/register",
        json={"name": "Bob", "email": "bob@zenith.test", "password": "password123"},
    )
    assert client.get("/api/accounts").get_json()["summary"]["count"] == 0
    assert client.get(f"/api/accounts/{account_id}").status_code == 404

    # owner still can
    assert auth_client.get(f"/api/accounts/{account_id}").status_code == 200


def test_get_account_details(auth_client):
    account_id = open_account(auth_client, "checking", "US", "Everyday")
    resp = auth_client.get(f"/api/accounts/{account_id}")
    assert resp.status_code == 200
    account = resp.get_json()["account"]
    for field in (
        "iban",
        "account_number",
        "routing_number",
        "balance_cents",
        "currency",
        "status",
        "created_at",
    ):
        assert field in account


def test_get_account_unknown_id_404(auth_client):
    assert auth_client.get("/api/accounts/99999").status_code == 404


def test_countries_endpoint(client):
    resp = client.get("/api/countries")
    assert resp.status_code == 200
    countries = resp.get_json()["countries"]
    assert len(countries) >= 240
    by_code = {c["code"]: c for c in countries}
    assert by_code["DE"]["iban"] is True
    assert by_code["US"]["iban"] is False
    assert by_code["AF"]["currency"] == "AFN"
    assert "NG" not in by_code


def test_account_can_be_opened_in_newly_supported_country(auth_client):
    resp = auth_client.post(
        "/api/accounts",
        json={"type": "checking", "country_code": "AF", "nickname": "Kabul"},
    )
    assert resp.status_code == 201
    assert resp.get_json()["account"]["currency"] == "AFN"
