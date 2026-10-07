"""The landing page and auth pages are served by Flask."""

from zenithpay import create_app

PAGES = {
    "/": ('id="signupModal"', "landing"),
    "/index.html": ('id="signupModal"', "landing"),
    "/login": ('id="login-email"', "login"),
    "/login.html": ('id="login-email"', "login"),
    "/register": ('id="reg-email"', "register"),
    "/register.html": ('id="reg-email"', "register"),
}


def test_pages_are_served(client):
    for path, (marker, _) in PAGES.items():
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.mimetype == "text/html", path
        assert marker in resp.get_data(as_text=True), path


def test_login_page_does_not_prefill_demo_credentials(client):
    body = client.get("/login").get_data(as_text=True)
    assert 'id="login-email" required value=' not in body
    assert 'id="login-password" required value=' not in body


def test_index_links_to_auth_pages_relatively(client):
    # Relative hrefs so the links also work when opened from the filesystem.
    body = client.get("/").get_data(as_text=True)
    assert 'href="login.html"' in body
    assert 'href="register.html"' in body
    assert 'href="/login"' not in body
    assert 'href="/register"' not in body


def test_static_assets_are_served(client):
    resp = client.get("/assets/index.html")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"


def test_shared_redesign_stylesheet_is_served_and_linked(client, logged_in_user):
    stylesheet = client.get("/assets/site.css")
    assert stylesheet.status_code == 200
    assert stylesheet.mimetype == "text/css"
    assert "--brand-dark" in stylesheet.get_data(as_text=True)

    for path in PAGES:
        body = client.get(path).get_data(as_text=True)
        assert 'href="/assets/site.css"' in body, path
    for path in MEMBER_PAGES:
        body = logged_in_user.get(path).get_data(as_text=True)
        assert 'href="/assets/site.css"' in body, path


def test_unknown_page_redirects_to_index(client):
    # Any non-API, non-asset, non-dashboard path sends the visitor back to the
    # landing page. The dashboard has its own handler (and its own auth gate).
    for path in ("/accounts/1", "/anything/else"):
        resp = client.get(path)
        assert resp.status_code == 302, path
        assert resp.headers["Location"] == "/", path


def test_dashboard_requires_auth(client):
    # Unauthenticated requests for the dashboard are sent to the sign-in page.
    resp = client.get("/dashboard")
    assert resp.status_code == 302, "GET /dashboard"
    assert resp.headers["Location"] == "/login", "GET /dashboard"


def test_dashboard_serves_page_when_authenticated(logged_in_user):
    resp = logged_in_user.get("/dashboard")
    assert resp.status_code == 200, ("GET /dashboard (logged in)", resp.get_data(as_text=True))
    assert resp.mimetype == "text/html", "GET /dashboard (logged in)"
    body = resp.get_data(as_text=True)
    assert 'id="account-cards"' in body, "GET /dashboard"
    assert 'My Accounts' in body or 'Your Accounts' in body, "GET /dashboard"


def test_dashboard_links_between_pages_are_relative(logged_in_user):
    body = logged_in_user.get("/dashboard").get_data(as_text=True)
    assert 'href="index.html"' in body
    assert 'href="transfer.html"' in body
    assert 'href="activity.html"' in body


def test_dashboard_header_keeps_profile_without_navigation(logged_in_user):
    body = logged_in_user.get("/dashboard").get_data(as_text=True)
    assert 'id="user-chip"' in body
    assert 'id="user-name"' in body
    assert 'id="user-email"' in body
    assert "<nav" not in body
    assert "Open navigation menu" not in body


# Every member feature page: path -> marker element unique to that page.
MEMBER_PAGES = {
    "/dashboard": 'id="account-cards"',
    "/transfer": 'id="form-transfer"',
    "/send": 'id="form-send"',
    "/bills": 'id="form-pay-bill"',
    "/scheduled": 'id="form-schedule"',
    "/activity": 'id="activity-list"',
    "/settings": 'id="profile-form"',
    "/crypto": 'id="buy-form"',
}


def test_member_pages_require_auth(client):
    # Feature pages are session-gated like the dashboard: no session means a
    # bounce to the sign-in page, for both the pretty and the .html URL.
    for path in MEMBER_PAGES:
        for url in (path, f"{path}.html"):
            resp = client.get(url)
            assert resp.status_code == 302, f"GET {url}"
            assert resp.headers["Location"] == "/login", f"GET {url}"


def test_member_pages_served_when_authenticated(logged_in_user):
    for path, marker in MEMBER_PAGES.items():
        resp = logged_in_user.get(path)
        assert resp.status_code == 200, f"GET {path}"
        assert resp.mimetype == "text/html", f"GET {path}"
        assert marker in resp.get_data(as_text=True), f"GET {path} missing {marker}"


def test_shared_app_script_is_served(client):
    # Pages load shared helpers via the relative src="app.js"; the /app.js
    # route keeps that working when Flask serves the pages.
    resp = client.get("/app.js")
    assert resp.status_code == 200
    assert "javascript" in (resp.mimetype or "")
    body = resp.get_data(as_text=True)
    assert "async function apiFetch" in body
    assert "async function safeRun" in body


def test_unknown_api_path_stays_json(client):
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "not found", "code": "not_found"}


def test_unknown_asset_path_stays_json(client):
    resp = client.get("/assets/does-not-exist.js")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "not found", "code": "not_found"}


def test_health_route_untouched(client):
    assert client.get("/api/health").get_json() == {"status": "ok"}


def test_service_worker_is_served(client):
    # The kill-switch worker is served at root scope instead of falling into
    # the catch-all redirect (a 302 here would break worker registration).
    resp = client.get("/service-worker.js")
    assert resp.status_code == 200
    assert "javascript" in (resp.mimetype or "")
    assert "unregister" in resp.get_data(as_text=True)


def test_fresh_database_auto_initializes(tmp_path):
    # A brand-new database must not need "flask init-db" first: create_app
    # applies schema.sql itself, so login works out of the box.
    app = create_app(test_config={
        "TESTING": True,
        "SECRET_KEY": "test",
        "DATABASE": str(tmp_path / "fresh.sqlite3"),
    })
    client = app.test_client()

    resp = client.post(
        "/api/auth/register",
        json={
            "name": "Fresh Member",
            "email": "fresh@zenith.test",
            "password": "password123",
        },
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)

    # The session from registration grants dashboard access immediately.
    assert client.get("/dashboard").status_code == 200
