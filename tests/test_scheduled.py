"""Payment scheduling: creation, lazy execution, retries, advancement."""
from datetime import timedelta

from tests.conftest import open_account
from zenithpay.db import get_db
from zenithpay.money import advance_due, utc_today


def fund(client, account_id, amount=1_000_000):
    resp = client.post(
        f"/api/accounts/{account_id}/transactions",
        json={"direction": "credit", "amount_cents": amount, "description": "Funding"},
    )
    assert resp.status_code == 201, resp.get_json()


def balance(client, account_id):
    return client.get(f"/api/accounts/{account_id}").get_json()["account"]["balance_cents"]


def schedule(client, **payload):
    base = {
        "kind": "transfer",
        "source_account_id": None,
        "dest_account_id": None,
        "amount_cents": 1_000,
        "description": "Scheduled move",
        "frequency": "once",
        "next_due_at": utc_today().isoformat(),
    }
    base.update(payload)
    return client.post("/api/scheduled-payments", json=base)


def test_create_validations(auth_client):
    checking = open_account(auth_client, "checking", "US", "Checking")
    savings = open_account(auth_client, "savings", "US", "Savings")
    loan = open_account(auth_client, "loan", "US", "Loan")
    tomorrow = (utc_today() + timedelta(days=1)).isoformat()

    assert schedule(auth_client, kind="magic").get_json()["code"] == "invalid_kind"
    assert schedule(
        auth_client, source_account_id=checking, dest_account_id=None
    ).get_json()["code"] == "invalid_destination"
    assert schedule(
        auth_client, source_account_id=checking, dest_account_id=savings, frequency="sometimes"
    ).get_json()["code"] == "invalid_frequency"
    assert schedule(
        auth_client, source_account_id=checking, dest_account_id=savings, next_due_at="01/02/2027"
    ).get_json()["code"] == "invalid_next_due_at"
    assert schedule(
        auth_client, source_account_id=checking, dest_account_id=savings,
        next_due_at=tomorrow, end_at=utc_today().isoformat(),
    ).get_json()["code"] == "invalid_end_at"
    assert schedule(
        auth_client, source_account_id=checking, dest_account_id=savings, amount_cents=0
    ).get_json()["code"] == "invalid_amount"
    assert schedule(
        auth_client, source_account_id=loan, dest_account_id=savings
    ).get_json()["code"] == "invalid_account_type"

    foreign = schedule(
        auth_client, source_account_id=424_242, dest_account_id=savings
    )
    assert foreign.status_code == 404


def test_due_transfer_executes_on_dashboard(auth_client):
    checking = open_account(auth_client, "checking", "US", "Checking")
    savings = open_account(auth_client, "savings", "US", "Savings")
    fund(auth_client, checking, 100_000)

    created = schedule(
        auth_client, source_account_id=checking, dest_account_id=savings,
        amount_cents=25_000, frequency="once",
    )
    assert created.status_code == 201

    dash = auth_client.get("/api/accounts").get_json()
    assert dash["scheduler"] == {"executed": 1, "failed": 0}
    assert balance(auth_client, checking) == 75_000
    assert balance(auth_client, savings) == 25_000

    rows = auth_client.get("/api/scheduled-payments").get_json()["scheduled_payments"]
    assert rows[0]["status"] == "done"
    assert rows[0]["last_error"] is None


def test_not_yet_due_does_not_run(auth_client):
    checking = open_account(auth_client, "checking", "US", "Checking")
    savings = open_account(auth_client, "savings", "US", "Savings")
    fund(auth_client, checking, 100_000)
    tomorrow = (utc_today() + timedelta(days=1)).isoformat()

    schedule(
        auth_client, source_account_id=checking, dest_account_id=savings,
        amount_cents=25_000, next_due_at=tomorrow,
    )
    dash = auth_client.get("/api/accounts").get_json()
    assert dash["scheduler"] == {"executed": 0, "failed": 0}
    assert balance(auth_client, savings) == 0

    rows = auth_client.get("/api/scheduled-payments").get_json()["scheduled_payments"]
    assert rows[0]["status"] == "active"
    assert rows[0]["next_due_at"] == tomorrow


def test_scheduled_p2p_and_force_run(auth_client, client):
    client.post(
        "/api/auth/register",
        json={"name": "Bob Member", "email": "bob@zenith.test", "password": "password123"},
    )
    bob_account = open_account(client, "checking", "US", "Bob Checking")
    ada_account = open_account(auth_client, "checking", "US", "Ada Checking")
    fund(auth_client, ada_account, 50_000)

    created = schedule(
        auth_client, kind="p2p", source_account_id=ada_account,
        recipient="bob@zenith.test", amount_cents=1_000,
        description="Coffee club dues", frequency="weekly",
    )
    assert created.status_code == 201

    run = auth_client.post("/api/scheduler/run").get_json()
    assert run["executed"] == 1 and run["failed"] == 0
    assert balance(auth_client, ada_account) == 49_000
    assert balance(client, bob_account) == 1_000

    # weekly: advanced one week, not done
    row = auth_client.get("/api/scheduled-payments").get_json()["scheduled_payments"][0]
    assert row["status"] == "active"
    assert row["next_due_at"] == (utc_today() + timedelta(days=7)).isoformat()

    # idempotent: nothing else is due
    again = auth_client.post("/api/scheduler/run").get_json()
    assert again["executed"] == 0


def test_insufficient_funds_retries_until_funded(auth_client):
    checking = open_account(auth_client, "checking", "US", "Checking")
    savings = open_account(auth_client, "savings", "US", "Savings")
    fund(auth_client, checking, 1_000)
    schedule(
        auth_client, source_account_id=checking, dest_account_id=savings,
        amount_cents=50_000, frequency="once",
    )

    first = auth_client.post("/api/scheduler/run").get_json()
    assert first["failed"] == 1
    assert first["results"][0]["error"]["code"] == "insufficient_funds"

    row = auth_client.get("/api/scheduled-payments").get_json()["scheduled_payments"][0]
    assert row["status"] == "active"          # not paused by failure
    assert "insufficient_funds" in row["last_error"]
    assert row["next_due_at"] == utc_today().isoformat()  # due date held for retry

    fund(auth_client, checking, 100_000)
    second = auth_client.post("/api/scheduler/run").get_json()
    assert second["executed"] == 1
    assert balance(auth_client, savings) == 50_000


def test_scheduled_bill_pay_uses_payee(auth_client):
    checking = open_account(auth_client, "checking", "US", "Checking")
    fund(auth_client, checking, 50_000)
    payee = auth_client.post(
        "/api/payees", json={"name": "Fiberline Internet", "category": "internet"}
    ).get_json()["payee"]

    schedule(
        auth_client, kind="bill_pay", source_account_id=checking,
        payee_id=payee["id"], amount_cents=4_999,
        description="Internet bill", frequency="monthly",
    )
    run = auth_client.post("/api/scheduler/run").get_json()
    assert run["executed"] == 1

    txns = auth_client.get(f"/api/accounts/{checking}/transactions").get_json()
    entry = txns["transactions"][0]
    assert entry["counterparty"] == "Fiberline Internet"
    assert entry["category"] == "internet"
    assert entry["direction"] == "debit"

    row = auth_client.get("/api/scheduled-payments").get_json()["scheduled_payments"][0]
    assert row["next_due_at"] == advance_due(utc_today(), "monthly").isoformat()


def test_end_at_marks_done(auth_client):
    checking = open_account(auth_client, "checking", "US", "Checking")
    savings = open_account(auth_client, "savings", "US", "Savings")
    fund(auth_client, checking, 100_000)
    schedule(
        auth_client, source_account_id=checking, dest_account_id=savings,
        amount_cents=1_000, frequency="monthly",
        next_due_at=utc_today().isoformat(), end_at=utc_today().isoformat(),
    )
    dash = auth_client.get("/api/accounts").get_json()
    assert dash["scheduler"]["executed"] == 1
    row = auth_client.get("/api/scheduled-payments").get_json()["scheduled_payments"][0]
    assert row["status"] == "done"  # next step would pass end_at


def test_advance_due_calendar_math():
    assert advance_due(utc_today(), "weekly") == utc_today() + timedelta(days=7)
    assert advance_due(utc_today(), "biweekly") == utc_today() + timedelta(days=14)
    from datetime import date

    assert advance_due(date(2026, 1, 31), "monthly") == date(2026, 2, 28)
    assert advance_due(date(2026, 10, 1), "monthly") == date(2026, 11, 1)
    assert advance_due(date(2026, 6, 15), "yearly") == date(2027, 6, 15)
    assert advance_due(date(2024, 2, 29), "yearly") == date(2025, 2, 28)


def test_pause_resume_and_cancel(auth_client):
    checking = open_account(auth_client, "checking", "US", "Checking")
    savings = open_account(auth_client, "savings", "US", "Savings")
    fund(auth_client, checking, 100_000)
    schedule_id = schedule(
        auth_client, source_account_id=checking, dest_account_id=savings,
        amount_cents=5_000,
    ).get_json()["scheduled_payment"]["id"]

    auth_client.patch(f"/api/scheduled-payments/{schedule_id}", json={"status": "paused"})
    dash = auth_client.get("/api/accounts").get_json()
    assert dash["scheduler"]["executed"] == 0

    auth_client.patch(f"/api/scheduled-payments/{schedule_id}", json={"status": "active"})
    dash = auth_client.get("/api/accounts").get_json()
    assert dash["scheduler"]["executed"] == 1
    assert balance(auth_client, savings) == 5_000

    # cancel path
    schedule_id = schedule(
        auth_client, source_account_id=checking, dest_account_id=savings,
        amount_cents=5_000,
    ).get_json()["scheduled_payment"]["id"]
    cancelled = auth_client.delete(f"/api/scheduled-payments/{schedule_id}")
    assert cancelled.get_json()["status"] == "cancelled"
    dash = auth_client.get("/api/accounts").get_json()
    assert dash["scheduler"]["executed"] == 0


def test_scheduler_is_scoped_to_user(auth_client, client):
    checking = open_account(auth_client, "checking", "US", "Checking")
    savings = open_account(auth_client, "savings", "US", "Savings")
    fund(auth_client, checking, 100_000)
    schedule(
        auth_client, source_account_id=checking, dest_account_id=savings,
        amount_cents=5_000,
    )

    client.post(
        "/api/auth/register",
        json={"name": "Bob Member", "email": "bob@zenith.test", "password": "password123"},
    )
    dash = client.get("/api/accounts").get_json()
    assert dash["scheduler"] == {"executed": 0, "failed": 0}  # Ada's schedule untouched
    assert balance(auth_client, savings) == 0  # Ada's money did not move

    # Ada's own access runs it (lazy execution by design)
    listing = auth_client.get("/api/scheduled-payments").get_json()
    assert listing["run_results"][0]["status"] == "executed"
    assert balance(auth_client, savings) == 5_000
    row = listing["scheduled_payments"][0]
    assert row["status"] == "done"

    # Bob cannot edit or delete Ada's schedule
    schedule_id = row["id"]
    assert client.patch(f"/api/scheduled-payments/{schedule_id}", json={"status": "paused"}).status_code == 404
    assert client.delete(f"/api/scheduled-payments/{schedule_id}").status_code == 404


def test_scheduler_endpoints_require_auth(client):
    assert client.post("/api/scheduler/run").status_code == 401
    assert client.get("/api/scheduled-payments").status_code == 401
    assert client.post("/api/scheduled-payments", json={}).status_code == 401


def test_list_status_filter(auth_client):
    checking = open_account(auth_client, "checking", "US", "Checking")
    savings = open_account(auth_client, "savings", "US", "Savings")
    schedule(
        auth_client, source_account_id=checking, dest_account_id=savings,
        next_due_at="2099-01-01",
    )
    active = auth_client.get("/api/scheduled-payments?status=active").get_json()
    assert len(active["scheduled_payments"]) == 1
    done = auth_client.get("/api/scheduled-payments?status=done").get_json()
    assert done["scheduled_payments"] == []
    assert auth_client.get("/api/scheduled-payments?status=bogus").status_code == 400
