"""Register / login / logout plus the login_required guard."""
import base64
import binascii
import functools
import re
import sqlite3

from flask import Blueprint, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .errors import api_error

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PIN_RE = re.compile(r"^[0-9]{6}$")


def hash_password(password: str) -> str:
    """Werkzeug default (scrypt) — salted, never plaintext."""
    return generate_password_hash(password)


def login_required(view):
    """401 JSON unless a session user exists; exposes g.user_id."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        if user_id is None:
            return api_error("authentication required", "auth_required", 401)
        g.user_id = user_id
        return view(*args, **kwargs)

    return wrapped


def _public_user(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "avatar_data": row["avatar_data"],
        "has_pin": bool(row["pin_hash"]),
        "created_at": row["created_at"],
    }


@bp.post("/register")
def register():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    email = str(data.get("email") or "").strip()
    password = str(data.get("password") or "")
    pin = data.get("pin")
    confirm_pin = data.get("confirm_pin")

    if not name or len(name) > 120:
        return api_error("name must be 1-120 characters", "invalid_name", 400)
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return api_error("invalid email address", "invalid_email", 400)
    if not 8 <= len(password) <= 200:
        return api_error("password must be 8-200 characters", "invalid_password", 400)
    if (pin is None) != (confirm_pin is None) or (
        pin is not None
        and (
            not isinstance(pin, str)
            or not _PIN_RE.fullmatch(pin)
            or confirm_pin != pin
        )
    ):
        return api_error(
            "PIN must be six digits and match its confirmation", "invalid_pin", 400
        )

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (name, email, password_hash, pin_hash) VALUES (?, ?, ?, ?)",
            (
                name,
                email,
                hash_password(password),
                generate_password_hash(pin) if pin is not None else None,
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return api_error("an account with this email already exists", "email_taken", 409)

    user = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    session.clear()
    session["user_id"] = user["id"]
    return jsonify({"user": _public_user(user)}), 201


@bp.post("/pin")
@login_required
def set_pin():
    data = request.get_json(silent=True) or {}
    pin = data.get("pin")
    confirm_pin = data.get("confirm_pin")
    if not isinstance(pin, str) or not _PIN_RE.fullmatch(pin) or confirm_pin != pin:
        return api_error(
            "PIN must be six digits and match its confirmation", "invalid_pin", 400
        )

    db = get_db()
    updated = db.execute(
        "UPDATE users SET pin_hash = ? WHERE id = ? AND pin_hash IS NULL",
        (generate_password_hash(pin), g.user_id),
    )
    if updated.rowcount == 0:
        db.rollback()
        user = db.execute("SELECT id FROM users WHERE id = ?", (g.user_id,)).fetchone()
        if user is None:
            return api_error("authentication required", "auth_required", 401)
        return api_error("PIN is already set", "pin_already_set", 409)
    db.commit()
    return jsonify({"has_pin": True})


@bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip()
    password = str(data.get("password") or "")

    user = None
    if email and password:
        user = get_db().execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
    # Same message either way — don't reveal which emails exist.
    if user is None or not check_password_hash(user["password_hash"], password):
        return api_error("invalid email or password", "invalid_credentials", 401)

    session.clear()
    session["user_id"] = user["id"]
    return jsonify({"user": _public_user(user)})


@bp.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.get("/me")
def me():
    user_id = session.get("user_id")
    if user_id is None:
        return api_error("authentication required", "auth_required", 401)
    user = get_db().execute(
        "SELECT id, name, email, role, avatar_data, pin_hash, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if user is None:
        session.clear()
        return api_error("authentication required", "auth_required", 401)
    return jsonify({"user": _public_user(user)})


@bp.patch("/profile")
@login_required
def update_profile():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    avatar = data.get("avatar_data")
    if not name or len(name) > 120:
        return api_error("name must be 1-120 characters", "invalid_name", 400)
    if avatar is not None:
        if not isinstance(avatar, str) or len(avatar) > 700_000:
            return api_error("profile image is too large", "invalid_avatar", 400)
        match = re.fullmatch(
            r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]*={0,2})", avatar
        )
        if not match:
            return api_error(
                "profile image must be PNG, JPEG, or WebP", "invalid_avatar", 400
            )
        try:
            image = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            return api_error("profile image encoding is invalid", "invalid_avatar", 400)
        if not image or len(image) > 512 * 1024:
            return api_error("profile image must be under 512 KB", "invalid_avatar", 400)
        image_type = match.group(1)
        valid_signature = (
            image.startswith(b"\x89PNG\r\n\x1a\n")
            if image_type == "png"
            else image.startswith(b"\xff\xd8\xff")
            if image_type == "jpeg"
            else image.startswith(b"RIFF") and image[8:12] == b"WEBP"
        )
        if not valid_signature:
            return api_error("profile image contents do not match its format", "invalid_avatar", 400)

    db = get_db()
    if avatar is None:
        db.execute("UPDATE users SET name = ? WHERE id = ?", (name, g.user_id))
    else:
        db.execute(
            "UPDATE users SET name = ?, avatar_data = ? WHERE id = ?",
            (name, avatar, g.user_id),
        )
    db.commit()
    user = db.execute(
        "SELECT id, name, email, role, avatar_data, pin_hash, created_at FROM users WHERE id = ?",
        (g.user_id,),
    ).fetchone()
    return jsonify({"user": _public_user(user)})
