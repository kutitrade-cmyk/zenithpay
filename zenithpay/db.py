"""sqlite3 access helpers.

One connection per request, WAL journal, foreign keys enforced.
Schema changes go through PRAGMA user_version migrations so existing
databases are upgraded in place instead of wiped.
"""
import sqlite3
from pathlib import Path

import click
from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA_VERSION = 5


def get_db():
    """Return the request-scoped sqlite3 connection."""
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate(db):
    """Bring an existing database up to SCHEMA_VERSION (additive only)."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return

    user_columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(users)").fetchall()
    }
    if user_columns and "role" not in user_columns:
        db.execute(
            "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'"
        )
    if user_columns and "avatar_data" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN avatar_data TEXT")
    if user_columns and "pin_hash" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN pin_hash TEXT")

    columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(transactions)").fetchall()
    }
    if columns and "transfer_group_id" not in columns:
        # v1 database: add the movement-linking column in place.
        db.execute("ALTER TABLE transactions ADD COLUMN transfer_group_id TEXT")
    if columns and "receipt_details" not in columns:
        db.execute("ALTER TABLE transactions ADD COLUMN receipt_details TEXT")


def init_db():
    """Create missing tables (schema.sql) and apply pending migrations."""
    db = get_db()
    _migrate(db)
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    db.executescript(schema)
    _provision_admin(db)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.commit()


def _provision_admin(db):
    """Create/promote the explicitly configured bootstrap administrator."""
    email = str(current_app.config.get("ADMIN_EMAIL") or "").strip().lower()
    password = str(current_app.config.get("ADMIN_PASSWORD") or "")
    if not email and not password:
        return
    if not email or len(password) < 12:
        raise ValueError(
            "Configure both ADMIN_EMAIL and an ADMIN_PASSWORD of at least 12 characters"
        )

    user = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if user:
        db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user["id"],))
    else:
        db.execute(
            "INSERT INTO users (email, name, password_hash, role) VALUES (?, ?, ?, 'admin')",
            (email, "Administrator", generate_password_hash(password)),
        )


@click.command("init-db")
def init_db_command():
    """flask init-db — create tables if missing, migrate older databases."""
    init_db()
    click.echo("Initialized the database.")


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    _ensure_schema(app)


def _ensure_schema(app):
    """Create tables on startup so a fresh checkout just works.

    Without this, the first login on a brand-new database dies with
    "no such table: users" until someone remembers to run ``flask
    init-db``. schema.sql is idempotent (IF NOT EXISTS everywhere), so
    running it against an existing database is harmless.
    """
    with app.app_context():
        try:
            init_db()
        except sqlite3.Error:
            # Database not reachable from here (missing directory,
            # read-only filesystem, ...). The first real request will
            # surface the same error it always did.
            pass
