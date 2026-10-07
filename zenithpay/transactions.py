"""Transaction history: searchable, filterable, paginated + recording entries."""
from datetime import date, timedelta

from flask import Blueprint, g, jsonify, request

from .auth import login_required
from .db import get_db
from .errors import api_error
from .money import MAX_AMOUNT_CENTS, serialize_transaction

bp = Blueprint("transactions", __name__, url_prefix="/api/accounts")
receipt_bp = Blueprint("receipts", __name__, url_prefix="/api/transactions")


def _int_arg(name):
    """Parse an optional integer query param -> (value, error_response)."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None, None
    try:
        return int(raw), None
    except ValueError:
        return None, api_error(f"{name} must be an integer", f"invalid_{name}", 400)


def _date_arg(name):
    """Parse an optional YYYY-MM-DD query param -> (date, error_response)."""
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None, None
    try:
        return date.fromisoformat(raw), None
    except ValueError:
        return None, api_error(f"{name} must be YYYY-MM-DD", f"invalid_{name}", 400)


@bp.get("/<int:account_id>/transactions")
@login_required
def list_transactions(account_id):
    """Filterable statement: search, date range, direction, category, amount, page."""
    db = get_db()
    account = db.execute(
        "SELECT id, balance_cents FROM accounts WHERE id = ? AND user_id = ?",
        (account_id, g.user_id),
    ).fetchone()
    if account is None:
        return api_error("account not found", "account_not_found", 404)

    where = ["account_id = ?"]
    params: list = [account_id]

    search = (request.args.get("search") or "").strip()
    if len(search) > 100:
        return api_error(
            "search term must be 100 characters or fewer", "search_too_long", 400
        )
    if search:
        escaped = (
            search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        where.append(
            "(description LIKE ? ESCAPE '\\'"
            " OR IFNULL(counterparty, '') LIKE ? ESCAPE '\\'"
            " OR IFNULL(category, '') LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern])

    direction = (request.args.get("direction") or "").lower().strip()
    if direction:
        if direction not in ("credit", "debit"):
            return api_error(
                "direction must be credit or debit", "invalid_direction", 400
            )
        where.append("direction = ?")
        params.append(direction)

    category = (request.args.get("category") or "").strip()
    if category:
        where.append("category = ?")
        params.append(category)

    from_date, err = _date_arg("from")
    if err:
        return err
    to_date, err = _date_arg("to")
    if err:
        return err
    if from_date and to_date and from_date > to_date:
        return api_error("from must be on or before to", "invalid_date_range", 400)
    if from_date:
        # created_at is fixed-width ISO 8601 -> lexicographic compare works
        where.append("created_at >= ?")
        params.append(from_date.isoformat())
    if to_date:
        where.append("created_at < ?")  # inclusive end-of-day
        params.append((to_date + timedelta(days=1)).isoformat())

    min_amount, err = _int_arg("min")
    if err:
        return err
    max_amount, err = _int_arg("max")
    if err:
        return err
    for name, value in (("min", min_amount), ("max", max_amount)):
        if value is not None and value <= 0:
            return api_error(
                f"{name} must be a positive integer", f"invalid_{name}", 400
            )
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        return api_error("min must be <= max", "invalid_amount_range", 400)
    if min_amount is not None:
        where.append("amount_cents >= ?")
        params.append(min_amount)
    if max_amount is not None:
        where.append("amount_cents <= ?")
        params.append(max_amount)

    limit, err = _int_arg("limit")
    if err:
        return err
    offset, err = _int_arg("offset")
    if err:
        return err
    limit = 50 if limit is None else limit
    offset = 0 if offset is None else offset
    if not 1 <= limit <= 200:
        return api_error("limit must be 1-200", "invalid_limit", 400)
    if offset < 0:
        return api_error("offset must be >= 0", "invalid_offset", 400)

    where_sql = " AND ".join(where)
    rows = db.execute(
        f"SELECT * FROM transactions WHERE {where_sql}"
        " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    total = db.execute(
        f"SELECT COUNT(*) AS n FROM transactions WHERE {where_sql}", params
    ).fetchone()["n"]

    return jsonify(
        {
            "transactions": [serialize_transaction(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "account_balance_cents": account["balance_cents"],
        }
    )


@receipt_bp.get("/<int:transaction_id>/receipt")
@login_required
def get_receipt(transaction_id):
    row = get_db().execute(
        """
        SELECT t.*, a.nickname, a.currency, a.account_number, a.iban, a.user_id
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.id = ?
        """,
        (transaction_id,),
    ).fetchone()
    if row is None:
        return api_error("transaction not found", "transaction_not_found", 404)
    if row["user_id"] != g.user_id:
        actor = get_db().execute(
            "SELECT role FROM users WHERE id = ?", (g.user_id,)
        ).fetchone()
        if actor is None or actor["role"] != "admin":
            return api_error("transaction not found", "transaction_not_found", 404)
    return jsonify(
        {
            "receipt": {
                **serialize_transaction(row),
                "account": {
                    "id": row["account_id"],
                    "nickname": row["nickname"],
                    "currency": row["currency"],
                    "account_number_last4": row["account_number"][-4:],
                    "iban": row["iban"],
                },
            }
        }
    )


@bp.post("/<int:account_id>/transactions")
@login_required
def create_transaction(account_id):
    """Record one entry. Balance update + insert are atomic (BEGIN IMMEDIATE).

    Ledger math: credit = +, debit = -.
    Checking/savings cannot go below zero; loans may (overpayment).
    """
    data = request.get_json(silent=True) or {}
    direction = str(data.get("direction") or "").lower().strip()
    amount = data.get("amount_cents")
    description = str(data.get("description") or "").strip()
    category = str(data.get("category") or "").strip() or None
    counterparty = str(data.get("counterparty") or "").strip() or None

    if direction not in ("credit", "debit"):
        return api_error("direction must be credit or debit", "invalid_direction", 400)
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return api_error(
            "amount_cents must be a positive integer number of cents",
            "invalid_amount",
            400,
        )
    if amount > MAX_AMOUNT_CENTS:
        return api_error("amount_cents exceeds the per-entry cap", "amount_too_large", 400)
    if not description or len(description) > 200:
        return api_error("description must be 1-200 characters", "invalid_description", 400)
    if category and len(category) > 40:
        return api_error("category must be 40 characters or fewer", "invalid_category", 400)
    if counterparty and len(counterparty) > 120:
        return api_error(
            "counterparty must be 120 characters or fewer", "invalid_counterparty", 400
        )

    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        account = db.execute(
            "SELECT type, status, balance_cents FROM accounts"
            " WHERE id = ? AND user_id = ?",
            (account_id, g.user_id),
        ).fetchone()
        if account is None:
            db.rollback()
            return api_error("account not found", "account_not_found", 404)
        if account["status"] != "active":
            db.rollback()
            return api_error("account is not active", "account_inactive", 409)

        delta = amount if direction == "credit" else -amount
        new_balance = account["balance_cents"] + delta
        if new_balance < 0 and account["type"] in ("checking", "savings"):
            db.rollback()
            return api_error("insufficient funds", "insufficient_funds", 400)

        cur = db.execute(
            "INSERT INTO transactions"
            " (account_id, direction, amount_cents, description, category,"
            "  counterparty, balance_after_cents)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                account_id,
                direction,
                amount,
                description,
                category,
                counterparty,
                new_balance,
            ),
        )
        db.execute(
            "UPDATE accounts SET balance_cents = ? WHERE id = ?",
            (new_balance, account_id),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    row = db.execute(
        "SELECT * FROM transactions WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return jsonify(
        {
            "transaction": serialize_transaction(row),
            "account_balance_cents": new_balance,
        }
    ), 201
