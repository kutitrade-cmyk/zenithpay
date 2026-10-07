"""Payees: saved billers + pay-now endpoint (Bill Pay)."""
import sqlite3

from flask import Blueprint, g, jsonify, request

from . import money
from .auth import login_required
from .db import get_db
from .errors import api_error

bp = Blueprint("payees", __name__, url_prefix="/api")


def _serialize(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "reference_number": row["reference_number"],
        "notes": row["notes"],
        "created_at": row["created_at"],
    }


def _fetch_owned(payee_id):
    row = get_db().execute(
        "SELECT * FROM payees WHERE id = ? AND user_id = ?", (payee_id, g.user_id)
    ).fetchone()
    if row is None:
        return None, api_error("payee not found", "payee_not_found", 404)
    return row, None


@bp.get("/payees")
@login_required
def list_payees():
    rows = get_db().execute(
        "SELECT * FROM payees WHERE user_id = ? ORDER BY name ASC", (g.user_id,)
    ).fetchall()
    return jsonify({"payees": [_serialize(row) for row in rows]})


@bp.post("/payees")
@login_required
def create_payee():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    category = str(data.get("category") or "other").strip() or "other"
    reference = str(data.get("reference_number") or "").strip() or None
    notes = str(data.get("notes") or "").strip() or None

    if not name or len(name) > 80:
        return api_error("name must be 1-80 characters", "invalid_name", 400)
    if len(category) > 40:
        return api_error("category must be 40 characters or fewer", "invalid_category", 400)
    if reference and len(reference) > 60:
        return api_error("reference_number must be 60 characters or fewer", "invalid_reference", 400)
    if notes and len(notes) > 500:
        return api_error("notes must be 500 characters or fewer", "invalid_notes", 400)

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO payees (user_id, name, category, reference_number, notes)"
            " VALUES (?, ?, ?, ?, ?)",
            (g.user_id, name, category, reference, notes),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return api_error("you already have a payee with that name", "duplicate_payee", 409)

    row = db.execute("SELECT * FROM payees WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify({"payee": _serialize(row)}), 201


@bp.patch("/payees/<int:payee_id>")
@login_required
def update_payee(payee_id):
    payee, err = _fetch_owned(payee_id)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    updates: dict[str, object] = {}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 80:
            return api_error("name must be 1-80 characters", "invalid_name", 400)
        updates["name"] = name
    if "category" in data:
        category = str(data.get("category") or "").strip()
        if not category or len(category) > 40:
            return api_error("category must be 1-40 characters", "invalid_category", 400)
        updates["category"] = category
    if "reference_number" in data:
        value = data.get("reference_number")
        updates["reference_number"] = str(value).strip() if value else None
    if "notes" in data:
        value = data.get("notes")
        updates["notes"] = str(value).strip() if value else None
    if not updates:
        return api_error("no updatable fields supplied", "no_updates", 400)

    db = get_db()
    assignments = ", ".join(f"{column} = ?" for column in updates)
    try:
        db.execute(
            f"UPDATE payees SET {assignments} WHERE id = ? AND user_id = ?",
            (*updates.values(), payee_id, g.user_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return api_error("you already have a payee with that name", "duplicate_payee", 409)

    row = db.execute("SELECT * FROM payees WHERE id = ?", (payee_id,)).fetchone()
    return jsonify({"payee": _serialize(row)})


@bp.delete("/payees/<int:payee_id>")
@login_required
def delete_payee(payee_id):
    payee, err = _fetch_owned(payee_id)
    if err:
        return err
    db = get_db()
    in_use = db.execute(
        """
        SELECT 1 FROM scheduled_payments
        WHERE payee_id = ? AND status IN ('active', 'paused') LIMIT 1
        """,
        (payee_id,),
    ).fetchone()
    if in_use:
        return api_error(
            "payee has scheduled payments — cancel them first",
            "payee_in_use",
            409,
        )
    db.execute(
        "UPDATE scheduled_payments SET payee_id = NULL WHERE payee_id = ?",
        (payee_id,),
    )
    db.execute("DELETE FROM payees WHERE id = ? AND user_id = ?", (payee_id, g.user_id))
    db.commit()
    return "", 204


@bp.post("/payees/<int:payee_id>/pay")
@login_required
def pay_payee(payee_id):
    """Bill Pay now: one debit, counterparty = payee name."""
    data = request.get_json(silent=True) or {}
    source_id = data.get("source_account_id")
    if not isinstance(source_id, int) or isinstance(source_id, bool):
        return api_error("source_account_id is required", "invalid_source", 400)
    description = str(data.get("description") or "").strip() or None
    if description and len(description) > 200:
        return api_error("description must be 200 characters or fewer", "invalid_description", 400)
    category = str(data.get("category") or "").strip() or None
    if category and len(category) > 40:
        return api_error("category must be 40 characters or fewer", "invalid_category", 400)

    try:
        result = money.bill_pay(
            get_db(),
            g.user_id,
            source_id,
            payee_id,
            data.get("amount_cents"),
            description,
            category,
        )
    except money.MoneyError as exc:
        return api_error(exc.message, exc.code, exc.status)
    return jsonify(result), 201
