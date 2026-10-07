"""Market-priced crypto trades recorded in the Zenith Pay ledger."""
import json
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from flask import Blueprint, current_app, g, jsonify, request

from .auth import login_required
from .db import get_db
from .errors import api_error
from .money import MAX_AMOUNT_CENTS

bp = Blueprint("crypto", __name__, url_prefix="/api/crypto")
UNITS_PER_COIN = 100_000_000
MAX_TRADE_VALUE_CENTS = MAX_AMOUNT_CENTS
ASSETS = {
    "BTC": {"name": "Bitcoin", "provider_id": "bitcoin"},
    "ETH": {"name": "Ethereum", "provider_id": "ethereum"},
    "SOL": {"name": "Solana", "provider_id": "solana"},
}


class MarketPriceError(Exception):
    """Raised when the upstream market price cannot be trusted."""


def fetch_market_prices():
    """Fetch a complete live USD quote set from CoinGecko."""
    ids = ",".join(asset["provider_id"] for asset in ASSETS.values())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    request_obj = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "ZenithPay/1.0"},
    )
    try:
        with urllib.request.urlopen(
            request_obj, timeout=current_app.config.get("CRYPTO_PRICE_TIMEOUT", 5)
        ) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise MarketPriceError("live price provider is unavailable") from exc

    prices = {}
    try:
        for symbol, asset in ASSETS.items():
            price = Decimal(str(payload[asset["provider_id"]]["usd"]))
            if not price.is_finite() or price <= 0:
                raise ValueError("invalid quote")
            prices[symbol] = price
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise MarketPriceError("live price provider returned invalid quotes") from exc
    return prices


def _portfolio(db, prices):
    rows = db.execute(
        "SELECT symbol, quantity_units FROM crypto_holdings WHERE user_id = ?",
        (g.user_id,),
    ).fetchall()
    holdings = {row["symbol"]: row["quantity_units"] for row in rows}
    return {
        symbol: {
            "name": asset["name"],
            "price_usd": str(prices[symbol]),
            "quantity_units": holdings.get(symbol, 0),
        }
        for symbol, asset in ASSETS.items()
    }


def _get_prices():
    try:
        return fetch_market_prices(), None
    except MarketPriceError:
        return None, api_error(
            "live market prices are temporarily unavailable",
            "market_unavailable",
            502,
        )


@bp.get("")
@login_required
def portfolio():
    prices, error = _get_prices()
    if error:
        return error
    db = get_db()
    accounts = db.execute(
        """
        SELECT id, nickname, currency, balance_cents FROM accounts
        WHERE user_id = ? AND type != 'loan' AND status = 'active'
        ORDER BY id
        """,
        (g.user_id,),
    ).fetchall()
    return jsonify(
        {
            "market": _portfolio(db, prices),
            "accounts": [dict(row) for row in accounts],
            "price_source": "CoinGecko",
            "prices_updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )


@bp.post("/buy")
@login_required
def buy():
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol") or "").upper()
    account_id = data.get("account_id")
    amount_cents = data.get("amount_cents")
    if symbol not in ASSETS:
        return api_error("unsupported coin", "invalid_symbol", 400)
    if type(account_id) is not int or account_id <= 0:
        return api_error("invalid account_id", "invalid_account_id", 400)
    if (
        type(amount_cents) is not int
        or amount_cents <= 0
        or amount_cents > MAX_TRADE_VALUE_CENTS
    ):
        return api_error("amount_cents must be a positive integer", "invalid_amount", 400)
    db = get_db()
    account = db.execute(
        """
        SELECT * FROM accounts
        WHERE id = ? AND user_id = ? AND type != 'loan'
        """,
        (account_id, g.user_id),
    ).fetchone()
    if account is None:
        return api_error("account not found", "account_not_found", 404)
    if account["status"] != "active":
        return api_error("account is not active", "account_inactive", 409)
    if account["currency"] != "USD":
        return api_error("crypto trades require a USD account", "currency_mismatch", 400)

    prices, error = _get_prices()
    if error:
        return error
    price = prices[symbol]
    quantity = int(
        (Decimal(amount_cents) * UNITS_PER_COIN / (price * 100)).to_integral_value(
            rounding=ROUND_DOWN
        )
    )
    if quantity <= 0:
        return api_error("trade amount is below the minimum", "amount_too_small", 400)

    try:
        changed = db.execute(
            """
            UPDATE accounts SET balance_cents = balance_cents - ?
            WHERE id = ? AND user_id = ? AND status = 'active' AND balance_cents >= ?
            """,
            (amount_cents, account_id, g.user_id, amount_cents),
        ).rowcount
        if not changed:
            db.rollback()
            return api_error("insufficient funds", "insufficient_funds", 400)
        balance = db.execute(
            "SELECT balance_cents FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()["balance_cents"]
        group_id = str(uuid.uuid4())
        transaction = db.execute(
            """
            INSERT INTO transactions
                (account_id, direction, amount_cents, description, category,
                 counterparty, balance_after_cents, transfer_group_id, receipt_details)
            VALUES (?, 'debit', ?, ?, 'crypto', ?, ?, ?, ?)
            """,
            (
                account_id,
                amount_cents,
                f"Buy {Decimal(quantity) / UNITS_PER_COIN:.8f} {symbol} @ ${price}/coin",
                symbol,
                balance,
                group_id,
                json.dumps(
                    {
                        "symbol": symbol,
                        "quantity_units": quantity,
                        "price_usd": str(price),
                    }
                ),
            ),
        )
        db.execute(
            """
            INSERT INTO crypto_holdings (user_id, symbol, quantity_units)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, symbol) DO UPDATE
                SET quantity_units = quantity_units + excluded.quantity_units
            """,
            (g.user_id, symbol, quantity),
        )
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise
    return jsonify(
        {
            "symbol": symbol,
            "quantity_units": quantity,
            "amount_cents": amount_cents,
            "balance_after_cents": balance,
            "price_usd": str(price),
            "transaction_id": transaction.lastrowid,
            "receipt_number": f"ZP-{transaction.lastrowid:010d}",
        }
    ), 201


@bp.post("/sell")
@login_required
def sell():
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol") or "").upper()
    account_id = data.get("account_id")
    quantity = data.get("quantity_units")
    if symbol not in ASSETS:
        return api_error("unsupported coin", "invalid_symbol", 400)
    if type(account_id) is not int or account_id <= 0:
        return api_error("invalid account_id", "invalid_account_id", 400)
    if (
        type(quantity) is not int
        or quantity <= 0
        or quantity > MAX_TRADE_VALUE_CENTS
    ):
        return api_error("quantity_units must be a positive integer", "invalid_quantity", 400)
    db = get_db()
    account = db.execute(
        """
        SELECT * FROM accounts
        WHERE id = ? AND user_id = ? AND type != 'loan'
        """,
        (account_id, g.user_id),
    ).fetchone()
    if account is None:
        return api_error("account not found", "account_not_found", 404)
    if account["status"] != "active":
        return api_error("account is not active", "account_inactive", 409)
    if account["currency"] != "USD":
        return api_error("crypto trades require a USD account", "currency_mismatch", 400)

    prices, error = _get_prices()
    if error:
        return error
    price = prices[symbol]
    proceeds = int(
        (Decimal(quantity) * price * 100 / UNITS_PER_COIN).to_integral_value(
            rounding=ROUND_DOWN
        )
    )
    if proceeds <= 0:
        return api_error("sale amount is below the minimum", "amount_too_small", 400)
    if proceeds > MAX_TRADE_VALUE_CENTS:
        return api_error("sale amount exceeds the per-entry cap", "amount_too_large", 400)

    try:
        changed = db.execute(
            """
            UPDATE crypto_holdings SET quantity_units = quantity_units - ?
            WHERE user_id = ? AND symbol = ? AND quantity_units >= ?
            """,
            (quantity, g.user_id, symbol, quantity),
        ).rowcount
        if not changed:
            db.rollback()
            return api_error("insufficient coin balance", "insufficient_coin", 400)
        credited = db.execute(
            """
            UPDATE accounts SET balance_cents = balance_cents + ?
            WHERE id = ? AND user_id = ? AND status = 'active' AND currency = 'USD'
            """,
            (proceeds, account_id, g.user_id),
        ).rowcount
        if not credited:
            db.rollback()
            return api_error("account is not active", "account_inactive", 409)
        balance = db.execute(
            "SELECT balance_cents FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()["balance_cents"]
        group_id = str(uuid.uuid4())
        transaction = db.execute(
            """
            INSERT INTO transactions
                (account_id, direction, amount_cents, description, category,
                 counterparty, balance_after_cents, transfer_group_id, receipt_details)
            VALUES (?, 'credit', ?, ?, 'crypto', ?, ?, ?, ?)
            """,
            (
                account_id,
                proceeds,
                f"Sell {Decimal(quantity) / UNITS_PER_COIN:.8f} {symbol} @ ${price}/coin",
                symbol,
                balance,
                group_id,
                json.dumps(
                    {
                        "symbol": symbol,
                        "quantity_units": quantity,
                        "price_usd": str(price),
                    }
                ),
            ),
        )
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise
    return jsonify(
        {
            "symbol": symbol,
            "quantity_units": quantity,
            "amount_cents": proceeds,
            "balance_after_cents": balance,
            "price_usd": str(price),
            "transaction_id": transaction.lastrowid,
            "receipt_number": f"ZP-{transaction.lastrowid:010d}",
        }
    ), 201
