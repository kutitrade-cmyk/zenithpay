-- Zenith Pay core schema (v2 — money movement)
-- Rule #1: money is integer cents. Never floats.

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    pin_hash      TEXT,
    role          TEXT    NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'admin')),
    avatar_data   TEXT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type           TEXT    NOT NULL CHECK (type IN ('checking', 'savings', 'loan')),
    nickname       TEXT    NOT NULL,
    country_code   TEXT    NOT NULL,                       -- ISO 3166-1 alpha-2, drives IBAN format
    currency       TEXT    NOT NULL DEFAULT 'USD',
    iban           TEXT    UNIQUE,                         -- NULL for non-IBAN countries
    account_number TEXT    NOT NULL UNIQUE,
    routing_number TEXT    NOT NULL,
    balance_cents  INTEGER NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'frozen', 'closed')),
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);

CREATE TABLE IF NOT EXISTS transactions (
    id                  INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    direction           TEXT    NOT NULL CHECK (direction IN ('credit', 'debit')),
    amount_cents        INTEGER NOT NULL CHECK (amount_cents > 0),
    description         TEXT    NOT NULL,
    category            TEXT,
    counterparty        TEXT,
    balance_after_cents INTEGER NOT NULL,                  -- statement-style running balance
    transfer_group_id   TEXT,                              -- links both sides of a movement
    receipt_details     TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_txn_account_created
    ON transactions(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_txn_transfer
    ON transactions(transfer_group_id);

-- Bill payees (community-internal labels + the member's reference number)
CREATE TABLE IF NOT EXISTS payees (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name             TEXT    NOT NULL,
    category         TEXT    NOT NULL DEFAULT 'other',     -- utilities, internet, rent, credit_card, ...
    reference_number TEXT,                                 -- contract/account no. with that biller
    notes            TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_payees_user ON payees(user_id);

-- Recurring / future payments (transfer | p2p | bill_pay)
CREATE TABLE IF NOT EXISTS scheduled_payments (
    id                INTEGER PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind              TEXT    NOT NULL CHECK (kind IN ('transfer', 'p2p', 'bill_pay')),
    dest_account_id   INTEGER REFERENCES accounts(id),      -- kind = 'transfer'
    recipient         TEXT,                                 -- kind = 'p2p': email or account number
    recipient_account_id INTEGER,                           -- kind = 'p2p', optional explicit target
    payee_id          INTEGER REFERENCES payees(id),        -- kind = 'bill_pay'
    amount_cents      INTEGER NOT NULL CHECK (amount_cents > 0),
    description       TEXT    NOT NULL,
    category          TEXT,
    frequency         TEXT    NOT NULL
                              CHECK (frequency IN ('once', 'weekly', 'biweekly', 'monthly', 'yearly')),
    next_due_at       TEXT    NOT NULL,                     -- YYYY-MM-DD (UTC)
    end_at            TEXT,                                 -- optional stop date (YYYY-MM-DD)
    status            TEXT    NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'paused', 'done', 'cancelled')),
    last_error        TEXT,
    last_run_at       TEXT,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_scheduled_user
    ON scheduled_payments(user_id, status, next_due_at);

CREATE TABLE IF NOT EXISTS crypto_holdings (
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol         TEXT    NOT NULL,
    quantity_units INTEGER NOT NULL DEFAULT 0 CHECK (quantity_units >= 0),
    PRIMARY KEY (user_id, symbol)
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id                 INTEGER PRIMARY KEY,
    admin_user_id      INTEGER NOT NULL REFERENCES users(id),
    affected_user_id   INTEGER NOT NULL REFERENCES users(id),
    account_id         INTEGER NOT NULL REFERENCES accounts(id),
    transaction_id    INTEGER NOT NULL REFERENCES transactions(id),
    direction          TEXT    NOT NULL CHECK (direction IN ('credit', 'debit')),
    amount_cents       INTEGER NOT NULL CHECK (amount_cents > 0),
    reason             TEXT    NOT NULL,
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_created
    ON admin_audit_log(created_at DESC);
