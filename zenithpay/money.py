"""Money movement engine.

Every transfer, P2P payment, bill pay, loan repayment and scheduled payment
flows through these functions. Invariants:

- one SQLite transaction (BEGIN IMMEDIATE) per movement — pairs are atomic
- integer cents, never floats
- balance_after_cents recorded on every entry (statement-style audit)
- credit = +, debit = −; checking/savings never go below zero (loans may)
- transfers only between same-currency asset (checking/savings) accounts;
  loans are only touched by repay_loan (which debits the debt down)
- scheduled payments re-run the same engine — no second copy of money math
"""
from __future__ import annotations

import calendar
import json
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

MAX_AMOUNT_CENTS = 10_000_000_000  # $100,000,000.00 sanity cap per entry
ASSET_TYPES = ("checking", "savings")
FREQUENCIES = ("once", "weekly", "biweekly", "monthly", "yearly")


class MoneyError(Exception):
    """Domain error carrying an HTTP-ish (message, code, status) triple."""

    def __init__(self, message: str, code: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def require_amount(amount) -> int:
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise MoneyError(
            "amount_cents must be a positive integer number of cents",
            "invalid_amount",
            400,
        )
    if amount > MAX_AMOUNT_CENTS:
        raise MoneyError("amount_cents exceeds the per-entry cap", "amount_too_large", 400)
    return amount


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def utc_iso_now() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{now.microsecond // 1000:03d}Z"


def serialize_transaction(row) -> dict:
    result = {
        "id": row["id"],
        "account_id": row["account_id"],
        "direction": row["direction"],
        "amount_cents": row["amount_cents"],
        "description": row["description"],
        "category": row["category"],
        "counterparty": row["counterparty"],
        "balance_after_cents": row["balance_after_cents"],
        "transfer_group_id": row["transfer_group_id"],
        "receipt_number": f"ZP-{row['id']:010d}",
        "created_at": row["created_at"],
    }
    if "receipt_details" in row.keys() and row["receipt_details"]:
        result["receipt_details"] = json.loads(row["receipt_details"])
    return result


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
@contextmanager
def _atomic(db):
    """One movement = one transaction; any error rolls the whole thing back."""
    try:
        db.execute("BEGIN IMMEDIATE")
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def load_account(db, account_id, *, user_id=None, must_be=None) -> dict:
    """Fetch an account or raise MoneyError (404 hides foreign accounts)."""
    row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None or (user_id is not None and row["user_id"] != user_id):
        raise MoneyError("account not found", "account_not_found", 404)
    account = dict(row)
    if must_be is not None and account["type"] not in must_be:
        raise MoneyError(
            f"account type must be {' or '.join(must_be)}", "invalid_account_type", 400
        )
    if account["status"] != "active":
        raise MoneyError("account is not active", "account_inactive", 409)
    return account


def require_same_currency(left, right):
    if left["currency"] != right["currency"]:
        raise MoneyError(
            "transfers only between accounts of the same currency",
            "currency_mismatch",
            400,
        )


def _apply(db, account, direction, amount, *, description, category, counterparty, group_id):
    """Write one entry and move the balance. Caller holds BEGIN IMMEDIATE."""
    delta = amount if direction == "credit" else -amount
    new_balance = account["balance_cents"] + delta
    if new_balance < 0 and account["type"] in ASSET_TYPES:
        raise MoneyError("insufficient funds", "insufficient_funds", 400)

    cur = db.execute(
        """
        INSERT INTO transactions
            (account_id, direction, amount_cents, description, category,
             counterparty, balance_after_cents, transfer_group_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account["id"],
            direction,
            amount,
            description,
            category,
            counterparty,
            new_balance,
            group_id,
        ),
    )
    db.execute(
        "UPDATE accounts SET balance_cents = ? WHERE id = ?",
        (new_balance, account["id"]),
    )
    account["balance_cents"] = new_balance
    return cur.lastrowid, new_balance


def _pair_result(db, group_id, source_balance):
    rows = db.execute(
        "SELECT * FROM transactions WHERE transfer_group_id = ? ORDER BY id ASC",
        (group_id,),
    ).fetchall()
    return {
        "transfer_group_id": group_id,
        "transactions": [serialize_transaction(row) for row in rows],
        "source_balance_cents": source_balance,
    }


# --------------------------------------------------------------------------- #
# Public movements
# --------------------------------------------------------------------------- #
def transfer_internal(db, user_id, source_id, dest_id, amount, description, category=None):
    """Move money between your own checking/savings accounts (same currency)."""
    require_amount(amount)
    if source_id == dest_id:
        raise MoneyError("source and destination must differ", "self_transfer", 400)
    group = uuid.uuid4().hex
    with _atomic(db):
        source = load_account(db, source_id, user_id=user_id, must_be=ASSET_TYPES)
        dest = load_account(db, dest_id, user_id=user_id, must_be=ASSET_TYPES)
        require_same_currency(source, dest)
        _apply(
            db, source, "debit", amount,
            description=description, category=category, counterparty=None, group_id=group,
        )
        _apply(
            db, dest, "credit", amount,
            description=description, category=category, counterparty=None, group_id=group,
        )
        result = _pair_result(db, group, source["balance_cents"])
    return result


def transfer_p2p(
    db,
    sender_id,
    source_id,
    recipient,
    amount,
    description,
    category="p2p",
    recipient_account_id=None,
):
    """Pay another member by email or account number."""
    require_amount(amount)
    raw = str(recipient or "").strip()
    if not raw:
        raise MoneyError(
            "recipient is required (email or account number)", "invalid_recipient", 400
        )
    group = uuid.uuid4().hex
    with _atomic(db):
        source = load_account(db, source_id, user_id=sender_id, must_be=ASSET_TYPES)
        target = resolve_recipient(db, raw, recipient_account_id)
        if target["user_id"] == sender_id or target["id"] == source["id"]:
            raise MoneyError("cannot send a P2P payment to yourself", "self_transfer", 400)
        require_same_currency(source, target)

        sender_name = db.execute(
            "SELECT name FROM users WHERE id = ?", (sender_id,)
        ).fetchone()["name"]
        target_name = db.execute(
            "SELECT name FROM users WHERE id = ?", (target["user_id"],)
        ).fetchone()["name"]

        _apply(
            db, source, "debit", amount,
            description=description, category=category,
            counterparty=target_name, group_id=group,
        )
        _apply(
            db, target, "credit", amount,
            description=description, category=category,
            counterparty=sender_name, group_id=group,
        )
        result = _pair_result(db, group, source["balance_cents"])
    return result


def resolve_recipient(db, raw, recipient_account_id=None) -> dict:
    """Account number first, then email (falls back to their checking account)."""
    account = db.execute(
        "SELECT * FROM accounts WHERE account_number = ?", (raw,)
    ).fetchone()
    if account is None and "@" in raw:
        user = db.execute("SELECT id FROM users WHERE email = ?", (raw,)).fetchone()
        if user is not None:
            if recipient_account_id is not None:
                account = db.execute(
                    "SELECT * FROM accounts WHERE id = ?", (recipient_account_id,)
                ).fetchone()
                if account is not None and account["user_id"] != user["id"]:
                    account = None
            else:
                account = db.execute(
                    """
                    SELECT * FROM accounts
                    WHERE user_id = ? AND type IN ('checking', 'savings')
                      AND status = 'active'
                    ORDER BY CASE type WHEN 'checking' THEN 0 ELSE 1 END,
                             created_at ASC, id ASC
                    LIMIT 1
                    """,
                    (user["id"],),
                ).fetchone()
    if account is None:
        raise MoneyError(
            "no member found with that email or account number",
            "recipient_not_found",
            404,
        )
    target = dict(account)
    if target["type"] not in ASSET_TYPES:
        raise MoneyError(
            "P2P requires a checking or savings account", "invalid_account_type", 400
        )
    if target["status"] != "active":
        raise MoneyError("recipient account is not active", "account_inactive", 409)
    if target["user_id"] is None:  # pragma: no cover - schema guarantees NOT NULL
        raise MoneyError("recipient not found", "recipient_not_found", 404)
    if recipient_account_id is not None and target["id"] != recipient_account_id:
        raise MoneyError(
            "recipient account not found for that member",
            "recipient_not_found",
            404,
        )
    return target


def bill_pay(db, user_id, source_id, payee_id, amount, description=None, category=None):
    """Pay one of your saved payees (utilities, rent, credit card, ...)."""
    require_amount(amount)
    with _atomic(db):
        source = load_account(db, source_id, user_id=user_id, must_be=ASSET_TYPES)
        payee = db.execute(
            "SELECT * FROM payees WHERE id = ? AND user_id = ?", (payee_id, user_id)
        ).fetchone()
        if payee is None:
            raise MoneyError("payee not found", "payee_not_found", 404)
        txn_id, balance = _apply(
            db,
            source,
            "debit",
            amount,
            description=description or f"Bill payment — {payee['name']}",
            category=category or payee["category"],
            counterparty=payee["name"],
            group_id=None,
        )
        row = db.execute(
            "SELECT * FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()
        result = {
            "transaction": serialize_transaction(row),
            "account_balance_cents": balance,
        }
    return result


def repay_loan(db, user_id, source_id, loan_id, amount, description=None):
    """Shrink a loan: debit the source account AND debit the loan (debt down)."""
    require_amount(amount)
    group = uuid.uuid4().hex
    with _atomic(db):
        source = load_account(db, source_id, user_id=user_id, must_be=ASSET_TYPES)
        loan = load_account(db, loan_id, user_id=user_id, must_be=("loan",))
        require_same_currency(source, loan)
        _apply(
            db, source, "debit", amount,
            description=description or "Loan repayment",
            category="repayment", counterparty=None, group_id=group,
        )
        _apply(
            db, loan, "debit", amount,
            description=description or "Loan repayment",
            category="repayment", counterparty=None, group_id=group,
        )
        result = _pair_result(db, group, source["balance_cents"])
    return result


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
def advance_due(due: date, frequency: str) -> date:
    """Calendar-correct step: Jan 31 monthly -> Feb 28."""
    if frequency == "weekly":
        return due + timedelta(days=7)
    if frequency == "biweekly":
        return due + timedelta(days=14)
    if frequency in ("monthly", "yearly"):
        months = 1 if frequency == "monthly" else 12
        total = due.year * 12 + (due.month - 1) + months
        year, month_zero = divmod(total, 12)
        month = month_zero + 1
        day = min(due.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    raise ValueError(f"cannot advance frequency {frequency!r}")


def run_due_payments(db, user_id, today=None) -> list[dict]:
    """Execute this user's due scheduled payments (lazy — call on API access).

    Each item runs the normal engine independently: successes advance
    next_due_at; failures keep next_due_at (self-healing retry) and record
    last_error on the row.
    """
    today = today or utc_today()
    rows = db.execute(
        """
        SELECT * FROM scheduled_payments
        WHERE user_id = ? AND status = 'active' AND next_due_at <= ?
        ORDER BY next_due_at ASC, id ASC
        """,
        (user_id, today.isoformat()),
    ).fetchall()
    return [_run_one(db, row, today) for row in rows]


def _run_one(db, row, today: date) -> dict:
    try:
        if row["kind"] == "transfer":
            result = transfer_internal(
                db, row["user_id"], row["source_account_id"], row["dest_account_id"],
                row["amount_cents"], row["description"], row["category"],
            )
            txn_ids = [txn["id"] for txn in result["transactions"]]
        elif row["kind"] == "p2p":
            result = transfer_p2p(
                db, row["user_id"], row["source_account_id"], row["recipient"],
                row["amount_cents"], row["description"], row["category"],
                row["recipient_account_id"],
            )
            txn_ids = [txn["id"] for txn in result["transactions"]]
        else:  # bill_pay
            result = bill_pay(
                db, row["user_id"], row["source_account_id"], row["payee_id"],
                row["amount_cents"], row["description"], row["category"],
            )
            txn_ids = [result["transaction"]["id"]]

        status = "active"
        next_due = row["next_due_at"]
        if row["frequency"] == "once":
            status = "done"
        else:
            stepped = advance_due(date.fromisoformat(row["next_due_at"]), row["frequency"])
            if row["end_at"] and stepped > date.fromisoformat(row["end_at"]):
                status = "done"
            else:
                next_due = stepped.isoformat()

        db.execute(
            """
            UPDATE scheduled_payments
            SET next_due_at = ?, status = ?, last_error = NULL, last_run_at = ?
            WHERE id = ?
            """,
            (next_due, status, utc_iso_now(), row["id"]),
        )
        db.commit()
        return {
            "id": row["id"],
            "status": "executed",
            "transaction_ids": txn_ids,
            "next_due_at": next_due,
            "schedule_status": status,
        }
    except MoneyError as exc:
        db.execute(
            "UPDATE scheduled_payments SET last_error = ?, last_run_at = ? WHERE id = ?",
            (f"{exc.code}: {exc.message}", utc_iso_now(), row["id"]),
        )
        db.commit()
        return {
            "id": row["id"],
            "status": "failed",
            "error": {"code": exc.code, "message": exc.message},
        }
