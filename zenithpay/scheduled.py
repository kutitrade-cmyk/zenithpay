"""Payment scheduling: CRUD for future/recurring payments + the runner."""
from datetime import date

from flask import Blueprint, g, jsonify, request

from . import money
from .auth import login_required
from .db import get_db
from .errors import api_error

bp = Blueprint("scheduled", __name__, url_prefix="/api")

KINDS = ("transfer", "p2p", "bill_pay")
STATUSES = ("active", "paused", "cancelled")  # settable via API
LIST_STATUSES = ("active", "paused", "done", "cancelled")


def _serialize(row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "source_account_id": row["source_account_id"],
        "source_nickname": row["source_nickname"],
        "dest_account_id": row["dest_account_id"],
        "recipient": row["recipient"],
        "recipient_account_id": row["recipient_account_id"],
        "payee_id": row["payee_id"],
        "payee_name": row["payee_name"],
        "amount_cents": row["amount_cents"],
        "description": row["description"],
        "category": row["category"],
        "frequency": row["frequency"],
        "next_due_at": row["next_due_at"],
        "end_at": row["end_at"],
        "status": row["status"],
        "last_error": row["last_error"],
        "last_run_at": row["last_run_at"],
        "created_at": row["created_at"],
    }


def _parse_date(value, field):
    text = str(value or "").strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text), None
    except ValueError:
        pass
    return None, api_error(f"{field} must be YYYY-MM-DD", f"invalid_{field}", 400)


_SELECT = """
    SELECT s.*, a.nickname AS source_nickname, p.name AS payee_name
    FROM scheduled_payments s
    JOIN accounts a ON a.id = s.source_account_id
    LEFT JOIN payees p ON p.id = s.payee_id
"""


@bp.post("/scheduled-payments")
@login_required
def create_scheduled():
    data = request.get_json(silent=True) or {}

    kind = str(data.get("kind") or "").lower().strip()
    if kind not in KINDS:
        return api_error(f"kind must be one of {', '.join(KINDS)}", "invalid_kind", 400)

    source_id = data.get("source_account_id")
    if not isinstance(source_id, int) or isinstance(source_id, bool):
        return api_error("source_account_id is required", "invalid_source", 400)

    description = str(data.get("description") or "").strip()
    if not description or len(description) > 200:
        return api_error("description must be 1-200 characters", "invalid_description", 400)
    category = str(data.get("category") or "").strip() or None
    if category and len(category) > 40:
        return api_error("category must be 40 characters or fewer", "invalid_category", 400)

    frequency = str(data.get("frequency") or "").lower().strip()
    if frequency not in money.FREQUENCIES:
        return api_error(
            f"frequency must be one of {', '.join(money.FREQUENCIES)}",
            "invalid_frequency",
            400,
        )

    next_due, err = _parse_date(data.get("next_due_at"), "next_due_at")
    if err:
        return err
    end_at = None
    if data.get("end_at") is not None:
        end_at, err = _parse_date(data.get("end_at"), "end_at")
        if err:
            return err
        if end_at < next_due:
            return api_error("end_at must be on or after next_due_at", "invalid_end_at", 400)

    db = get_db()
    dest_id = recipient = recipient_account_id = payee_id = None
    try:
        amount = money.require_amount(data.get("amount_cents"))
        # Validate now so bad schedules fail fast, not at 3am.
        source = money.load_account(db, source_id, user_id=g.user_id, must_be=money.ASSET_TYPES)

        if kind == "transfer":
            dest_id = data.get("dest_account_id")
            if not isinstance(dest_id, int) or isinstance(dest_id, bool):
                return api_error("dest_account_id is required", "invalid_destination", 400)
            if dest_id == source_id:
                return api_error("source and destination must differ", "self_transfer", 400)
            dest = money.load_account(db, dest_id, user_id=g.user_id, must_be=money.ASSET_TYPES)
            money.require_same_currency(source, dest)
        elif kind == "p2p":
            recipient = str(data.get("recipient") or "").strip()
            if not recipient:
                return api_error(
                    "recipient is required (email or account number)",
                    "invalid_recipient",
                    400,
                )
            recipient_account_id = data.get("recipient_account_id")
            if recipient_account_id is not None and (
                not isinstance(recipient_account_id, int)
                or isinstance(recipient_account_id, bool)
            ):
                return api_error(
                    "recipient_account_id must be an integer",
                    "invalid_recipient_account",
                    400,
                )
            target = money.resolve_recipient(db, recipient, recipient_account_id)
            money.require_same_currency(source, target)
        else:  # bill_pay
            payee_id = data.get("payee_id")
            if not isinstance(payee_id, int) or isinstance(payee_id, bool):
                return api_error("payee_id is required", "invalid_payee", 400)
            payee = db.execute(
                "SELECT id FROM payees WHERE id = ? AND user_id = ?",
                (payee_id, g.user_id),
            ).fetchone()
            if payee is None:
                raise money.MoneyError("payee not found", "payee_not_found", 404)

        cur = db.execute(
            """
            INSERT INTO scheduled_payments
                (user_id, source_account_id, kind, dest_account_id, recipient,
                 recipient_account_id, payee_id, amount_cents, description,
                 category, frequency, next_due_at, end_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                g.user_id, source_id, kind, dest_id, recipient,
                recipient_account_id, payee_id, amount, description,
                category, frequency, next_due.isoformat(),
                end_at.isoformat() if end_at else None,
            ),
        )
        db.commit()
    except money.MoneyError as exc:
        db.rollback()
        return api_error(exc.message, exc.code, exc.status)

    row = db.execute(_SELECT + " WHERE s.id = ?", (cur.lastrowid,)).fetchone()
    return jsonify({"scheduled_payment": _serialize(row)}), 201


@bp.get("/scheduled-payments")
@login_required
def list_scheduled():
    # Lazy execution: due items run before we list, so state is fresh.
    results = money.run_due_payments(get_db(), g.user_id)

    status = str(request.args.get("status") or "").lower().strip()
    if status and status not in LIST_STATUSES:
        return api_error("status must be active, paused, done or cancelled", "invalid_status", 400)

    db = get_db()
    if status:
        rows = db.execute(
            _SELECT + " WHERE s.user_id = ? AND s.status = ?"
            " ORDER BY s.next_due_at ASC, s.id ASC",
            (g.user_id, status),
        ).fetchall()
    else:
        rows = db.execute(
            _SELECT + " WHERE s.user_id = ?"
            " ORDER BY CASE s.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,"
            " s.next_due_at ASC, s.id ASC",
            (g.user_id,),
        ).fetchall()
    return jsonify(
        {
            "scheduled_payments": [_serialize(row) for row in rows],
            "run_results": results,
        }
    )


@bp.patch("/scheduled-payments/<int:schedule_id>")
@login_required
def update_scheduled(schedule_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM scheduled_payments WHERE id = ? AND user_id = ?",
        (schedule_id, g.user_id),
    ).fetchone()
    if row is None:
        return api_error("scheduled payment not found", "scheduled_not_found", 404)

    data = request.get_json(silent=True) or {}
    updates: dict[str, object] = {}

    if "status" in data:
        status = str(data.get("status") or "").lower().strip()
        if status not in STATUSES:
            return api_error(
                "status must be active, paused or cancelled", "invalid_status", 400
            )
        updates["status"] = status
    if "amount_cents" in data:
        try:
            updates["amount_cents"] = money.require_amount(data.get("amount_cents"))
        except money.MoneyError as exc:
            return api_error(exc.message, exc.code, exc.status)
    if "description" in data:
        description = str(data.get("description") or "").strip()
        if not description or len(description) > 200:
            return api_error("description must be 1-200 characters", "invalid_description", 400)
        updates["description"] = description
    if "frequency" in data:
        frequency = str(data.get("frequency") or "").lower().strip()
        if frequency not in money.FREQUENCIES:
            return api_error("invalid frequency", "invalid_frequency", 400)
        updates["frequency"] = frequency
    if "next_due_at" in data:
        next_due, err = _parse_date(data.get("next_due_at"), "next_due_at")
        if err:
            return err
        updates["next_due_at"] = next_due.isoformat()
    if "end_at" in data:
        if data.get("end_at") is None:
            updates["end_at"] = None
        else:
            end_at, err = _parse_date(data.get("end_at"), "end_at")
            if err:
                return err
            updates["end_at"] = end_at.isoformat()
    if not updates:
        return api_error("no updatable fields supplied", "no_updates", 400)

    assignments = ", ".join(f"{column} = ?" for column in updates)
    db.execute(
        f"UPDATE scheduled_payments SET {assignments} WHERE id = ? AND user_id = ?",
        (*updates.values(), schedule_id, g.user_id),
    )
    db.commit()

    fresh = db.execute(_SELECT + " WHERE s.id = ?", (schedule_id,)).fetchone()
    return jsonify({"scheduled_payment": _serialize(fresh)})


@bp.delete("/scheduled-payments/<int:schedule_id>")
@login_required
def cancel_scheduled(schedule_id):
    db = get_db()
    cur = db.execute(
        "UPDATE scheduled_payments SET status = 'cancelled' WHERE id = ? AND user_id = ?",
        (schedule_id, g.user_id),
    )
    db.commit()
    if cur.rowcount == 0:
        return api_error("scheduled payment not found", "scheduled_not_found", 404)
    return jsonify({"id": schedule_id, "status": "cancelled"})


@bp.post("/scheduler/run")
@login_required
def run_scheduler():
    """Force-run this member's due payments (call from cron if you like)."""
    results = money.run_due_payments(get_db(), g.user_id)
    executed = sum(1 for item in results if item["status"] == "executed")
    failed = len(results) - executed
    return jsonify({"executed": executed, "failed": failed, "results": results})
