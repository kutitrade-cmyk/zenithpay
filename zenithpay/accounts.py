"""Account dashboard, account details, and account creation."""
import sqlite3

from flask import Blueprint, g, jsonify, request

from .auth import login_required
from .db import get_db
from .errors import api_error
from .money import run_due_payments
from .iban import (
    default_currency,
    format_iban,
    generate_account_number,
    generate_iban,
    generate_routing_number,
    is_supported,
    supports_iban,
    supported_countries,
)

bp = Blueprint("accounts", __name__, url_prefix="/api")

ACCOUNT_TYPES = ("checking", "savings", "loan")


def serialize_account(row) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "nickname": row["nickname"],
        "country_code": row["country_code"],
        "currency": row["currency"],
        "iban": format_iban(row["iban"]) if row["iban"] else None,
        "account_number": row["account_number"],
        "routing_number": row["routing_number"],
        "balance_cents": row["balance_cents"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


@bp.get("/countries")
def countries():
    """Supported countries for account creation."""
    return jsonify({"countries": supported_countries()})


@bp.get("/accounts")
@login_required
def list_accounts():
    """Dashboard: run due scheduled payments first, then totals per currency."""
    runs = run_due_payments(get_db(), g.user_id)
    rows = get_db().execute(
        """
        SELECT * FROM accounts
        WHERE user_id = ? AND status != 'closed'
        ORDER BY created_at ASC, id ASC
        """,
        (g.user_id,),
    ).fetchall()

    accounts = [serialize_account(row) for row in rows]
    totals: dict[str, int] = {}
    for account in accounts:
        totals[account["currency"]] = (
            totals.get(account["currency"], 0) + account["balance_cents"]
        )
    return jsonify(
        {
            "accounts": accounts,
            "summary": {
                "count": len(accounts),
                "balance_cents_by_currency": totals,
            },
            "scheduler": {
                "executed": sum(1 for r in runs if r["status"] == "executed"),
                "failed": sum(1 for r in runs if r["status"] == "failed"),
            },
        }
    )


@bp.post("/accounts")
@login_required
def create_account():
    """Open a new account; identifiers (IBAN / number / routing) are generated."""
    data = request.get_json(silent=True) or {}
    account_type = str(data.get("type") or "").lower().strip()
    country = str(data.get("country_code") or "").upper().strip()
    nickname = str(data.get("nickname") or "").strip()
    currency = str(data.get("currency") or "").upper().strip()

    if account_type not in ACCOUNT_TYPES:
        return api_error(
            f"type must be one of {', '.join(ACCOUNT_TYPES)}", "invalid_type", 400
        )
    if not is_supported(country):
        return api_error(
            "unsupported country_code (see GET /api/countries)",
            "unsupported_country",
            400,
        )
    if not nickname or len(nickname) > 80:
        return api_error("nickname must be 1-80 characters", "invalid_nickname", 400)
    if not currency:
        currency = default_currency(country)
    elif len(currency) != 3 or not currency.isalpha():
        return api_error("currency must be a 3-letter code", "invalid_currency", 400)

    db = get_db()
    user = db.execute("SELECT pin_hash FROM users WHERE id = ?", (g.user_id,)).fetchone()
    if user is None or not user["pin_hash"]:
        return api_error(
            "set a six-digit PIN before opening accounts", "pin_required", 403
        )

    for _ in range(6):  # retry on the (astronomically rare) identifier collision
        iban = generate_iban(country) if supports_iban(country) else None
        account_number = generate_account_number(country)
        routing_number = generate_routing_number(country)
        try:
            cur = db.execute(
                """
                INSERT INTO accounts
                    (user_id, type, nickname, country_code, currency, iban,
                     account_number, routing_number, balance_cents)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    g.user_id,
                    account_type,
                    nickname,
                    country,
                    currency,
                    iban,
                    account_number,
                    routing_number,
                ),
            )
            db.commit()
            break
        except sqlite3.IntegrityError:
            db.rollback()
    else:
        return api_error(
            "could not allocate unique account identifiers, try again",
            "allocation_failed",
            500,
        )

    row = db.execute(
        "SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return jsonify({"account": serialize_account(row)}), 201


@bp.get("/accounts/<int:account_id>")
@login_required
def get_account(account_id):
    """Account details: IBAN, account number, routing info, balance."""
    row = get_db().execute(
        "SELECT * FROM accounts WHERE id = ? AND user_id = ?",
        (account_id, g.user_id),
    ).fetchone()
    if row is None:
        return api_error("account not found", "account_not_found", 404)
    return jsonify({"account": serialize_account(row)})


@bp.get("/accounts/<int:account_id>/history")
@login_required
def get_account_history(account_id):
    """Recent transactions for one of the user's accounts (statement order)."""
    row = get_db().execute(
        "SELECT * FROM accounts WHERE id = ? AND user_id = ?",
        (account_id, g.user_id),
    ).fetchone()
    if row is None:
        return api_error("account not found", "account_not_found", 404)
    rows = get_db().execute(
        """
        SELECT * FROM transactions
        WHERE account_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 50
        """,
        (account_id,),
    ).fetchall()
    return jsonify(
        {
            "account_id": account_id,
            "transactions": [
                {
                    "id": r["id"],
                    "direction": r["direction"],
                    "amount_cents": r["amount_cents"],
                    "description": r["description"],
                    "category": r["category"],
                    "counterparty": r["counterparty"],
                    "balance_after_cents": r["balance_after_cents"],
                    "transfer_group_id": r["transfer_group_id"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }
    )
