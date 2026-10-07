"""Shared fixtures: fresh temp database per test."""
import pytest

from zenithpay import create_app
from zenithpay.db import init_db


@pytest.fixture
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(tmp_path / "test.sqlite3"),
        }
    )
    with app.app_context():
        init_db()
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_client(app):
    """Separate registered + logged-in client for Ada Member.

    Deliberately not built from the `client` fixture: tests that use both
    must see two independent sessions (Bob's login must not clobber Ada's).
    """
    client = app.test_client()
    resp = client.post(
        "/api/auth/register",
        json={
            "name": "Ada Member",
            "email": "ada@zenith.test",
            "password": "password123",
            "pin": "246810",
            "confirm_pin": "246810",
        },
    )
    assert resp.status_code == 201
    return client


def open_account(client, account_type="checking", country="US", nickname="Main"):
    """Helper: create an account, return its id."""
    user = client.get("/api/auth/me").get_json()["user"]
    if not user["has_pin"]:
        pin = client.post(
            "/api/auth/pin",
            json={"pin": "246810", "confirm_pin": "246810"},
        )
        assert pin.status_code == 200
    resp = client.post(
        "/api/accounts",
        json={
            "type": account_type,
            "country_code": country,
            "nickname": nickname,
        },
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["account"]["id"]


@pytest.fixture
def logged_in_user(app):
    """A client registered + logged in as a normal member, for dashboard tests."""
    client = app.test_client()
    resp = client.post(
        "/api/auth/register",
        json={
            "name": "Dashboard User",
            "email": "dashboard@zenith.test",
            "password": "password123",
            "pin": "246810",
            "confirm_pin": "246810",
        },
    )
    assert resp.status_code == 201, resp.get_json()
    return client
