"""Serve the Zenith Pay frontend.

The frontend is three static pages — a landing page, a sign-in page, and a
sign-up page. They share an origin with the JSON API under ``/api``, so the
``SameSite=Lax`` session cookie flows with every ``fetch`` and there is no CORS
plumbing to add.

Each page is reachable both without an extension (``/login``) and with one
(``/login.html``). The ``.html`` names matter because the pages link to each
other with relative hrefs: that way the links also work when a page is opened
straight from the filesystem on a phone, not just when Flask serves it.

Every other page request is routed back to the landing page, so a visitor who
asks for a page that does not exist ends up at the index instead of a 404.
"""
from pathlib import Path

from flask import Blueprint, current_app, redirect, send_from_directory
from werkzeug.exceptions import NotFound

bp = Blueprint("web", __name__)


def _static_dir() -> Path:
    return Path(current_app.static_folder)


def _page(filename: str):
    return send_from_directory(_static_dir(), filename)


@bp.get("/")
@bp.get("/index.html")
def index():
    """Serve the landing page."""
    return _page("index.html")


@bp.get("/login")
@bp.get("/login.html")
def login():
    """Serve the sign-in page."""
    return _page("login.html")


@bp.get("/register")
@bp.get("/register.html")
def register():
    """Serve the sign-up page."""
    return _page("register.html")



@bp.get("/dashboard")
@bp.get("/dashboard.html")
def dashboard():
    """Overview: balance tiles, quick actions, accounts, recent activity."""
    return _member_page("dashboard.html")


@bp.get("/transfer")
@bp.get("/transfer.html")
def transfer():
    """Move money between the member's own accounts."""
    return _member_page("transfer.html")


@bp.get("/send")
@bp.get("/send.html")
def send():
    """P2P payments to another member."""
    return _member_page("send.html")


@bp.get("/bills")
@bp.get("/bills.html")
def bills():
    """Bill pay: saved payees and pay-now."""
    return _member_page("bills.html")


@bp.get("/scheduled")
@bp.get("/scheduled.html")
def scheduled():
    """Recurring and future payments."""
    return _member_page("scheduled.html")


@bp.get("/activity")
@bp.get("/activity.html")
def activity():
    """Full transaction history with an account filter."""
    return _member_page("activity.html")


@bp.get("/settings")
@bp.get("/settings.html")
def settings():
    return _member_page("settings.html")


@bp.get("/crypto")
@bp.get("/crypto.html")
def crypto():
    return _member_page("crypto.html")


@bp.get("/admin")
@bp.get("/admin.html")
def admin():
    from flask import session

    user_id = session.get("user_id")
    if user_id is None:
        return redirect("/login")
    from .db import get_db

    user = get_db().execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or user["role"] != "admin":
        return redirect("/dashboard")
    return _page("admin.html")


def _member_page(filename: str):
    """Serve a member-only page, or send visitors to the sign-in page.

    Every feature page is session-gated here: without a session the member
    cannot even load the shell, let alone call the API behind it.
    """
    from flask import session

    if session.get("user_id") is None:
        return redirect("/login")
    return _page(filename)


@bp.get("/app.js")
def app_js():
    """Serve the shared member-app script.

    Pages reference it as the relative "app.js" so it also resolves when a
    page is opened straight from the filesystem; when served by Flask this
    route keeps that same relative path working over HTTP.
    """
    return _page("app.js")


@bp.get("/service-worker.js")
def service_worker():
    """Serve the service-worker kill switch at root scope.

    Service workers are scoped to their own path, so this must live at
    /service-worker.js — /assets/service-worker.js could not control the
    pages. The file itself unregisters stale workers and clears their
    caches; it must be routed before the catch-all redirect below.
    """
    return _page("service-worker.js")


@bp.get("/<path:page>")
def fallback(page):
    """Send any other page request to the index.

    API and asset paths are never shadowed — they keep their own handlers (or
    their JSON 404). Everything else is a page, and unknown pages land on the
    landing screen.
    """
    if page.startswith(("api/", "assets/")):
        raise NotFound()
    return redirect("/")
