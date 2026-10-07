# Zenith Pay 💳

Community bank app — a static landing/sign-in page plus a JSON API on
**Flask + SQLite3**. Money is stored as **integer cents** everywhere; never
floats.

Works anywhere Python runs — including **Android via Termux** (this project is
developed on a phone).

---

## Features

**Accounts**
- Dashboard with all accounts (Checking, Savings, Loans) + per-currency totals
- Dashboard cash-flow visualization for account credits and debits over the last 14 days
- Six-digit account PIN required before opening accounts; PINs are stored as salted password hashes
- Account details: IBAN, account number, routing number
- Open new accounts — IBAN / numbers are auto-generated with real check digits
- Multi-country (~80 IBAN countries; US routing numbers carry a valid ABA checksum)

**Money movement**
- Internal transfers (atomic pair — both entries or neither)
- P2P to another member by email *or* account number
- Loan repayments
- Bill Pay with a personal payee book

**History & scheduling**
- Searchable, filterable statement (search, date range, direction, category, min/max)
- Statement-style audit: every row stores `balance_after_cents`
- Scheduled/recurring payments with a lazy scheduler that self-heals on retry

**Profile & administration**
- Profile settings with PNG/JPEG/WebP image uploads (512 KB maximum)
- Optional administrator account configured by deployment settings; admin
  overview is read-only and ordinary sign-ups cannot grant themselves access

**Crypto sandbox**
- Current BTC, ETH, and SOL USD reference prices from CoinGecko
- Buy/sell entries use the latest live quote and post to the Zenith Pay account
  ledger; no exchange execution, crypto wallet custody, or withdrawal is provided

---

## Requirements

- Python **3.11+** (Termux: `pkg install python`)
- pip

## Setup

> **Termux/Android?** Work inside the project folder for *every* step below.
> Paste commands **one line at a time** — pasting `cd X && ...` together with
> the next command is the #1 cause of `cd: too many arguments`.

### 1. Get into the project

```bash
cd /storage/emulated/0/Download/ZenithPay
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
```

(Termux path is `/data/data/com.termux/files/usr/bin/python3` — same command.)

### 3. Install dependencies

```bash
.venv/bin/pip install -r requirements.txt
```

`flask`, `pytest`, `Babel`, and `pycountry` — no build step, no node_modules.
Babel and pycountry provide the ISO country names and current default currencies
for the account-opening country list; Nigeria is intentionally excluded.

The sign-in form starts blank; demo credentials are not prefilled. New members
set and confirm a six-digit PIN during registration. Existing members without
a PIN are prompted to set one in the dashboard before opening accounts. PINs
are stored only as salted hashes. To enable
the administrator account, configure `ADMIN_EMAIL` and `ADMIN_PASSWORD` in
`instance/config.py` before starting the app. Use a unique password with at
least 12 characters. On startup the app provisions that account and grants
the read-only admin overview; regular registrations always receive the member
role. Never commit administrator credentials.

Example local-only configuration:

```python
ADMIN_EMAIL = "admin@example.test"
ADMIN_PASSWORD = "use-a-unique-password-of-at-least-12-characters"
```

The member navigation includes Profile Settings (name and profile photo) and
Crypto. Market quotes come from CoinGecko. Trades use those live reference
prices but are recorded only in the Zenith Pay ledger; they do not execute
against an exchange or move real cryptocurrency. If the live price provider is
unavailable, price lookup and trades fail explicitly rather than using an old
or made-up price.

### 4. Create the database + demo data

```bash
.venv/bin/flask --app zenithpay init-db   # creates tables (runs migrations too)
.venv/bin/python seed.py                  # demo members, payees, scheduled bills
```

Use `--fresh` to wipe and reseed: `.venv/bin/python seed.py --fresh`

### 5. Run the server

```bash
.venv/bin/python -m flask --app zenithpay run --debug --port 5000
```

Open **http://127.0.0.1:5000** for the landing page, **`/login`** to sign in,
and **`/register`** to open an account. The JSON API lives under `/api` — check
it with `curl http://127.0.0.1:5000/api/health`. Any other page request
redirects back to `/`.

**Demo logins:** `demo@zenith.pay` and `neighbor@zenith.pay`
(password `password123`)

> Fun detail: the first dashboard load after seeding runs two due scheduled
> payments (electric bill + coffee club P2P) — that's the lazy scheduler at work.

### Running in the background (optional)

Handy on Termux so the server survives switching apps:

```bash
setsid nohup .venv/bin/python -m flask --app zenithpay run --debug --port 5000 > flask.log 2>&1 < /dev/null &
```

- Logs: `tail -f flask.log`
- Stop it: `pkill -f "flask --app zenithpay"`

To reach the server from another device on the same Wi-Fi, bind to all
interfaces: add `--host 0.0.0.0` (debug mode on a LAN is fine for dev only).

## Tests

```bash
.venv/bin/python -m pytest
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | liveness |
| POST | `/api/auth/register` | create account, starts session |
| POST | `/api/auth/login` | start session |
| POST | `/api/auth/logout` | end session |
| GET | `/api/auth/me` | current user |
| POST | `/api/auth/pin` | set a six-digit PIN once for existing members |
| PATCH | `/api/auth/profile` | update member name and optional validated profile image |
| GET | `/api/admin/overview` | admin-only platform and account overview |
| POST | `/api/admin/adjustments` | admin-only audited credit/debit adjustment |
| GET | `/api/crypto` | live CoinGecko USD quotes, holdings, and active accounts |
| POST | `/api/crypto/buy` | debit USD account at latest live reference quote |
| POST | `/api/crypto/sell` | credit USD account at latest live reference quote |
| GET | `/api/countries` | supported countries (IBAN flag + currency) |
| GET | `/api/accounts` | dashboard: accounts + per-currency totals |
| POST | `/api/accounts` | open account after setting a six-digit PIN (auto-generates IBAN / number / routing) |
| GET | `/api/accounts/<id>` | details: IBAN, account number, routing |
| GET | `/api/accounts/<id>/transactions` | history: `?search=&from=&to=&direction=&category=&min=&max=&limit=&offset=` |
| POST | `/api/accounts/<id>/transactions` | record a credit/debit entry |
| GET | `/api/transactions/<id>/receipt` | owner/admin-only transaction receipt |
| POST | `/api/transfers` | internal transfer (same currency, atomic pair) |
| POST | `/api/p2p` | pay another member by email **or** account number |
| POST | `/api/repayments` | shrink a loan (debits source + loan together) |
| GET/POST | `/api/payees` | Bill Pay payee book |
| PATCH/DELETE | `/api/payees/<id>` | edit / remove (blocked while scheduled) |
| POST | `/api/payees/<id>/pay` | pay a bill now |
| GET/POST | `/api/scheduled-payments` | list (runs due items first) / schedule a payment |
| PATCH/DELETE | `/api/scheduled-payments/<id>` | pause/resume/edit / cancel |
| POST | `/api/scheduler/run` | force-run your due payments (cron-friendly) |

Errors are always `{"error": "<message>", "code": "<snake_case>"}` with an HTTP status.

## Design rules

- **Cents as integers** — `amount_cents`, `balance_cents`. No floats, ever.
- **Statement-style audit** — every transaction stores `balance_after_cents`.
- **Atomic ledger math** — insert + balance update run in one `BEGIN IMMEDIATE`
  transaction. Credit = `+`, debit = `−`.
- **Overdraft blocked** on checking/savings (`insufficient_funds`); loan balances
  may go negative (overpayment).
- **Multi-country** — IBAN registry formats with real mod-97 check digits for
  ~80 countries; routing/account-only for the rest. Nigeria is intentionally
  not supported.
- **Auth** — scrypt-hashed passwords, httpOnly `SameSite=Lax` session cookie,
  `login_required` on every account/transaction route. Accounts are strictly
  per-user (foreign ids return 404).
- **One engine** — transfers, P2P, bill pay, repayments and scheduled payments
  all flow through `money.py`; each movement is one `BEGIN IMMEDIATE` pair —
  both entries or neither.
- **Scheduler is lazy** — due items run on dashboard/list access and via
  `POST /api/scheduler/run`. Failures hold their due date and set `last_error`
  (self-healing retry once funds arrive); successes advance `next_due_at`
  calendar-correctly (Jan 31 monthly → Feb 28).
- **Movement rules** — same-currency only; loans only via the repayment
  endpoint; P2P lands in the recipient's checking (or a chosen account).
- **Migrations** — `PRAGMA user_version`-based; `flask init-db` upgrades older
  databases in place, adding roles, profiles, crypto holdings, audit history,
  and receipt metadata without replacing existing transactions.

## Project layout

```text
zenithpay/
  __init__.py     app factory + blueprint wiring
  db.py           sqlite helpers (WAL, FK on) + migrations + `flask init-db`
  errors.py       shared error envelope
  auth.py         register/login/logout/me + login_required
  iban.py         IBAN registry, mod-97 engine, routing generators
  money.py        THE ENGINE: transfer/p2p/bill_pay/repay + scheduler runner
  accounts.py     dashboard (runs due schedules), details, creation
  transactions.py history with filters + single-entry recording
  transfers.py    /api/transfers, /api/p2p, /api/repayments
  crypto.py       live CoinGecko reference prices + account-ledger trades
  admin.py        role-gated admin overview + audited balance adjustments
  payees.py       payee CRUD + bill pay now
  scheduled.py    scheduled payment CRUD + /api/scheduler/run
  web.py          serves /, /login, /register; other pages redirect to /
  static/         index.html, login.html, register.html (served under /assets)
seed.py           demo data: 2 members, payees, scheduled bills
tests/            80 tests (IBAN, auth, accounts, filters, transfers/P2P/repay,
                   payees, scheduling, isolation, web serving)
```

## Troubleshooting

| Symptom | Cause & fix |
| --- | --- |
| `cd: too many arguments` | You pasted `cd ...` and the next command as one line. Run `cd` first, then the command. |
| `ModuleNotFoundError: flask` | Wrong interpreter — use `.venv/bin/python` / `.venv/bin/flask`, not bare `python`. |
| `address already in use` (port 5000) | A server is already running: `pkill -f "flask --app zenithpay"`, or pick another port. |
| `database is locked` | Another process holds the DB (WAL usually prevents this). Close the other server/process. |
| Server dies right after starting in background | It wasn't detached. Use the `setsid nohup ... &` line from **Running in the background**. |
| `linker: library ... not accessible` warning on Termux | Harmless Termux/Python 3.13 noise. Ignore it. |

## Roadmap

- `SESSION_COOKIE_SECURE = True` once serving over HTTPS.
- Deposit / top-up endpoint so a newly registered member can fund an account
  (accounts start at zero; `seed.py` funds the demo data with raw SQL).
- Push notifications (needs a schema bump and VAPID keys).
