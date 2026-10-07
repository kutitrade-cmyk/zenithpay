#!/usr/bin/env python3
"""Seed demo data for Zenith Pay.

Usage:
    python seed.py           # seed if not already present
    python seed.py --fresh   # delete the database and reseed from scratch

Demo logins:
    demo@zenith.pay / password123     (everything)
    neighbor@zenith.pay / password123 (P2P receiver)
"""
import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash

from zenithpay import create_app
from zenithpay.db import get_db, init_db
from zenithpay.iban import generate_account_number, generate_iban, generate_routing_number, supports_iban

DEMO_EMAIL = "demo@zenith.pay"
NEIGHBOR_EMAIL = "neighbor@zenith.pay"
PASSWORD = "password123"

DEMO_ACCOUNTS = [
    {"type": "checking", "nickname": "Everyday Checking", "country": "US", "currency": "USD"},
    {"type": "savings", "nickname": "Rainy Day Savings", "country": "GB", "currency": "GBP"},
    {"type": "loan", "nickname": "Community Loan", "country": "DE", "currency": "EUR"},
]
NEIGHBOR_ACCOUNTS = [
    {"type": "checking", "nickname": "Neighbor Checking", "country": "US", "currency": "USD"},
]

# nickname -> (days_ago, direction, amount_cents, description, category, counterparty)
HISTORY = {
    "Everyday Checking": [
        (28, "credit", 300_000, "Opening deposit", "deposit", "Zenith Pay Teller"),
        (26, "debit", 4_299, "Weekly groceries", "groceries", "Fresh Market"),
        (24, "credit", 250_000, "Salary", "income", "Acme Cooperative"),
        (21, "debit", 120_000, "Apartment rent", "housing", "Hilltop Properties"),
        (18, "debit", 3_550, "Coffee and pastry", "food", "Blue Bird Cafe"),
        (15, "credit", 15_000, "Gift from Grandma", "gift", "R. Whitfield"),
        (12, "debit", 8_999, "Phone bill", "utilities", "Ripple Mobile"),
        (9, "debit", 2_450, "Bus pass", "transport", "City Transit"),
        (6, "debit", 6_720, "Pharmacy", "health", "CarePlus Pharmacy"),
        (4, "debit", 5_000, "Lunch split", "p2p", "Neighbor Member"),
        (3, "credit", 250_000, "Salary", "income", "Acme Cooperative"),
        (1, "debit", 5_310, "Lunch", "food", "Corner Deli"),
    ],
    "Rainy Day Savings": [
        (30, "credit", 150_000, "Opening deposit", "deposit", "Zenith Pay Teller"),
        (20, "credit", 50_000, "Monthly savings", "savings", "Standing order"),
        (10, "credit", 1_450, "Interest", "interest", "Zenith Pay"),
        (2, "credit", 50_000, "Monthly savings", "savings", "Standing order"),
    ],
    "Community Loan": [
        (25, "credit", 500_000, "Loan disbursement", "loan", "Zenith Pay Lending"),
        (15, "debit", 12_500, "Repayment", "repayment", "Direct Debit"),
        (5, "debit", 12_500, "Repayment", "repayment", "Direct Debit"),
    ],
    "Neighbor Checking": [
        (12, "credit", 120_000, "Opening deposit", "deposit", "Zenith Pay Teller"),
        (4, "credit", 5_000, "Lunch split", "p2p", "Demo Member"),
    ],
}

# name, category, reference_number, notes
PAYEES = [
    ("PowerGrid Electric", "utilities", "PG-4417-88", None),
    ("Fiberline Internet", "internet", "FL-9921-04", None),
    ("Apex Credit Card", "credit_card", "AX-77-3391", None),
    ("Hilltop Properties", "rent", "HP-RENT-12B", "Apartment rent — unit 12B"),
]


def iso_timestamp(dt: datetime) -> str:
    """Match schema.sql's strftime('%Y-%m-%dT%H:%M:%fZ') shape exactly."""
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{dt.microsecond // 1000:03d}Z"


def insert_account(db, user_id, spec):
    country = spec["country"]
    iban = generate_iban(country) if supports_iban(country) else None
    account_id = db.execute(
        """
        INSERT INTO accounts
            (user_id, type, nickname, country_code, currency, iban,
             account_number, routing_number, balance_cents)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            user_id, spec["type"], spec["nickname"], country, spec["currency"],
            iban, generate_account_number(country), generate_routing_number(country),
        ),
    ).lastrowid
    return account_id, iban


def fill_history(db, account_id, rows, now):
    """Insert entries oldest-first so balance_after chains correctly."""
    balance = 0
    for days_ago, direction, amount, description, category, counterparty in sorted(
        rows, key=lambda row: -row[0]
    ):
        balance += amount if direction == "credit" else -amount
        db.execute(
            """
            INSERT INTO transactions
                (account_id, direction, amount_cents, description,
                 category, counterparty, balance_after_cents, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, direction, amount, description,
                category, counterparty, balance, iso_timestamp(now - timedelta(days=days_ago)),
            ),
        )
    db.execute("UPDATE accounts SET balance_cents = ? WHERE id = ?", (balance, account_id))
    return balance


def first_of_next_month(day: date) -> date:
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true", help="delete the database first")
    args = parser.parse_args()

    app = create_app()

    if args.fresh:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(app.config["DATABASE"]) + suffix)
            if path.exists():
                path.unlink()

    with app.app_context():
        init_db()
        db = get_db()

        if db.execute("SELECT 1 FROM users WHERE email = ?", (DEMO_EMAIL,)).fetchone():
            print("Demo data already present — run with --fresh to recreate.")
            return

        now = datetime.now(timezone.utc)
        today = now.date()

        demo_id = db.execute(
            "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
            ("Demo Member", DEMO_EMAIL, generate_password_hash(PASSWORD)),
        ).lastrowid
        neighbor_id = db.execute(
            "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
            ("Neighbor Member", NEIGHBOR_EMAIL, generate_password_hash(PASSWORD)),
        ).lastrowid

        print(f"Logins (password {PASSWORD}): {DEMO_EMAIL}, {NEIGHBOR_EMAIL}\n")

        print("Accounts")
        for owner_id, specs in ((demo_id, DEMO_ACCOUNTS), (neighbor_id, NEIGHBOR_ACCOUNTS)):
            for spec in specs:
                account_id, iban = insert_account(db, owner_id, spec)
                balance = fill_history(db, account_id, HISTORY[spec["nickname"]], now)
                print(f"  {spec['nickname']} ({spec['type']}, {spec['country']}, {spec['currency']})")
                print(f"    balance: {balance / 100:,.2f}")
                print(f"    iban:    {iban or '-'}")

        print("\nPayees")
        payee_ids = {}
        for name, category, reference, notes in PAYEES:
            payee_ids[name] = db.execute(
                "INSERT INTO payees (user_id, name, category, reference_number, notes)"
                " VALUES (?, ?, ?, ?, ?)",
                (demo_id, name, category, reference, notes),
            ).lastrowid
            print(f"  {name} ({category}, ref {reference})")

        checking = db.execute(
            "SELECT id FROM accounts WHERE user_id = ? AND nickname = 'Everyday Checking'",
            (demo_id,),
        ).fetchone()["id"]

        scheduled = [
            # kind, extra columns..., description, category, frequency, next_due
            dict(kind="bill_pay", payee_id=payee_ids["PowerGrid Electric"],
                 amount=8_999, description="Electric bill", category="utilities",
                 frequency="monthly", next_due=today.isoformat()),
            dict(kind="p2p", recipient=NEIGHBOR_EMAIL,
                 amount=1_000, description="Coffee club dues", category="social",
                 frequency="weekly", next_due=today.isoformat()),
            dict(kind="bill_pay", payee_id=payee_ids["Hilltop Properties"],
                 amount=120_000, description="Apartment rent", category="housing",
                 frequency="monthly", next_due=first_of_next_month(today).isoformat()),
        ]
        print("\nScheduled payments")
        for item in scheduled:
            db.execute(
                """
                INSERT INTO scheduled_payments
                    (user_id, source_account_id, kind, dest_account_id, recipient,
                     recipient_account_id, payee_id, amount_cents, description,
                     category, frequency, next_due_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    demo_id, checking, item["kind"], item.get("dest_account_id"),
                    item.get("recipient"), None, item.get("payee_id"),
                    item["amount"], item["description"], item["category"],
                    item["frequency"], item["next_due"],
                ),
            )
            due = "TODAY (runs on first dashboard load)" if item["next_due"] == today.isoformat() else item["next_due"]
            print(f"  {item['description']}: {item['amount'] / 100:,.2f} "
                  f"{item['frequency']} due {due}")

        db.commit()
        print("\nDone.")


if __name__ == "__main__":
    main()
