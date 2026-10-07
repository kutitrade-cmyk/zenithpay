"""Administrator overview, balance adjustments, and role checks."""
import functools
import json

from flask import Blueprint, g, jsonify, request

from .auth import login_required
from .db import get_db
from .errors import api_error
from .money import MAX_AMOUNT_CENTS, utc_iso_now

bp = Blueprint("admin", __name__, url_prefix="/api/admin")


def admin_required(view):
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        user = get_db().execute(
            "SELECT role FROM users WHERE id = ?", (g.user_id,)
        ).fetchone()
        if user is None or user["role"] != "admin":
            return api_error("administrator access required", "admin_required", 403)
        return view(*args, **kwargs)

    return wrapped


@bp.get("/overview")
@admin_required
def overview():
    db = get_db()
    totals = db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM users) AS users,
            (SELECT COUNT(*) FROM accounts WHERE status != 'closed') AS accounts,
            (SELECT COUNT(*) FROM transactions) AS transactions
        """
    ).fetchone()
    users = db.execute(
        "SELECT id, name, email, role, created_at FROM users ORDER BY created_at DESC"
    ).fetchall()
    accounts = db.execute(
        """
        SELECT a.id, a.user_id, a.nickname, a.type, a.currency, a.balance_cents,
               a.status, u.name AS user_name, u.email
        FROM accounts a JOIN users u ON u.id = a.user_id
        WHERE a.status != 'closed'
        ORDER BY u.name, a.id
        """
    ).fetchall()
    adjustments = db.execute(
        """
        SELECT l.id, l.direction, l.amount_cents, l.reason, l.created_at,
               u.email AS affected_user_email, a.nickname AS account_nickname,
               a.currency, t.id AS transaction_id
        FROM admin_audit_log l
        JOIN users u ON u.id = l.affected_user_id
        JOIN accounts a ON a.id = l.account_id
        JOIN transactions t ON t.id = l.transaction_id
        ORDER BY l.id DESC LIMIT 50
        """
    ).fetchall()
    return jsonify(
        {
            "summary": dict(totals),
            "users": [dict(row) for row in users],
            "accounts": [dict(row) for row in accounts],
            "adjustments": [dict(row) for row in adjustments],
        }
    )


@bp.post("/adjustments")
@admin_required
def adjust_balance():
    data = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    direction = str(data.get("direction") or "").strip().lower()
    amount = data.get("amount_cents")
    reason = str(data.get("reason") or "").strip()
    if type(account_id) is not int or account_id <= 0:
        return api_error("invalid account_id", "invalid_account_id", 400)
    if direction not in ("credit", "debit"):
        return api_error("direction must be credit or debit", "invalid_direction", 400)
    if type(amount) is not int or amount <= 0:
        return api_error("amount_cents must be a positive integer", "invalid_amount", 400)
    if amount > MAX_AMOUNT_CENTS:
        return api_error("amount_cents exceeds the per-entry cap", "amount_too_large", 400)
    if not reason or len(reason) > 200:
        return api_error("reason must be 1-200 characters", "invalid_reason", 400)

    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        account = db.execute(
            """
            SELECT a.*, u.email AS owner_email
            FROM accounts a JOIN users u ON u.id = a.user_id
            WHERE a.id = ? AND a.type IN ('checking', 'savings')
            """,
            (account_id,),
        ).fetchone()
        if account is None:
            db.rollback()
            return api_error("account not found", "account_not_found", 404)
        if account["status"] != "active":
            db.rollback()
            return api_error("account is not active", "account_inactive", 409)
        delta = amount if direction == "credit" else -amount
        balance = account["balance_cents"] + delta
        if balance < 0:
            db.rollback()
            return api_error("insufficient funds", "insufficient_funds", 400)
        actor = db.execute(
            "SELECT email FROM users WHERE id = ?", (g.user_id,)
        ).fetchone()
        updated = db.execute(
            """
            UPDATE accounts SET balance_cents = ?
            WHERE id = ? AND status = 'active' AND balance_cents = ?
            """,
            (balance, account_id, account["balance_cents"]),
        ).rowcount
        if not updated:
            db.rollback()
            return api_error("account changed; retry the adjustment", "balance_changed", 409)
        cur = db.execute(
            """
            INSERT INTO transactions
                (account_id, direction, amount_cents, description, category,
                 counterparty, balance_after_cents, created_at, receipt_details)
            VALUES (?, ?, ?, ?, 'admin_adjustment', ?, ?, ?, ?)
            """,
            (
                account_id,
                direction,
                amount,
                reason,
                f"Admin: {actor['email']}",
                balance,
                utc_iso_now(),
                json.dumps(
                    {"admin_email": actor["email"], "reason": reason},
                    ensure_ascii=False,
                ),
            ),
        )
        db.execute(
            """
            INSERT INTO admin_audit_log
                (admin_user_id, affected_user_id, account_id, transaction_id,
                 direction, amount_cents, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (g.user_id, account["user_id"], account_id, cur.lastrowid, direction, amount, reason),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    transaction = db.execute(
        "SELECT * FROM transactions WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    from .money import serialize_transaction

    return jsonify(
        {
            "transaction": serialize_transaction(transaction),
            "account_balance_cents": balance,
            "affected_user_email": account["owner_email"],
        }
    ), 201
