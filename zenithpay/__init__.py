"""Zenith Pay — community bank backend."""
import os
from pathlib import Path

from flask import Flask
from werkzeug.exceptions import HTTPException

from . import db
from .errors import api_error

BASE_DIR = Path(__file__).resolve().parent.parent


def create_app(test_config=None):
    """Application factory for Zenith Pay."""
    # static assets live in zenithpay/static, served under /assets; web.bp serves
    # the landing page at /.
    app = Flask(
        __name__,
        instance_relative_config=True,
        static_folder="static",
        static_url_path="/assets",
    )

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        ADMIN_EMAIL=os.environ.get("ADMIN_EMAIL", ""),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", ""),
        DATABASE=str(BASE_DIR / "zenithpay.sqlite3"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # SESSION_COOKIE_SECURE = True  # enable once serving over HTTPS
        MAX_CONTENT_LENGTH=1024 * 1024,  # Includes bounded profile image uploads
    )

    if test_config is None:
        app.config.from_pyfile("config.py", silent=True)
        app.config.from_envvar("ZENITHPAY_CONFIG", silent=True)
    else:
        app.config.from_mapping(test_config)
    if test_config is not None:
        app.config.setdefault("ADMIN_EMAIL", "")
        app.config.setdefault("ADMIN_PASSWORD", "")

    db.init_app(app)

    from . import (
        accounts,
        admin,
        auth,
        crypto,
        payees,
        scheduled,
        transactions,
        transfers,
        web,
    )

    app.register_blueprint(web.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(crypto.bp)
    app.register_blueprint(accounts.bp)
    app.register_blueprint(transactions.bp)
    app.register_blueprint(transactions.receipt_bp)
    app.register_blueprint(transfers.bp)
    app.register_blueprint(payees.bp)
    app.register_blueprint(scheduled.bp)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.errorhandler(HTTPException)
    def http_error(err):
        """Keep the API envelope for framework errors (404, 405, ...)."""
        message = (err.name or "error").lower()
        return api_error(message, message.replace(" ", "_"), err.code or 500)

    return app
