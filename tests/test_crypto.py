"""Live market quotes price the account-ledger crypto trades."""
from decimal import Decimal

from tests.conftest import open_account


def fund(client, account_id, amount):
    return client.post(
        f"/api/accounts/{account_id}/transactions",
        json={"direction": "credit", "amount_cents": amount, "description": "Test funding"},
    )


def test_buy_and_sell_change_usd_balance_and_holdings(auth_client, monkeypatch):
    monkeypatch.setattr(
        "zenithpay.crypto.fetch_market_prices",
        lambda: {
            "BTC": Decimal("65000"),
            "ETH": Decimal("3500"),
            "SOL": Decimal("150"),
        },
    )
    account_id = open_account(auth_client, "checking", "US", "USD checking")
    assert fund(auth_client, account_id, 1_000_000).status_code == 201

    bought = auth_client.post(
        "/api/crypto/buy",
        json={"symbol": "BTC", "account_id": account_id, "amount_cents": 650_000},
    )
    assert bought.status_code == 201
    assert bought.get_json()["quantity_units"] == 10_000_000
    assert bought.get_json()["price_usd"] == "65000"
    assert auth_client.get(f"/api/accounts/{account_id}").get_json()["account"]["balance_cents"] == 350_000

    portfolio = auth_client.get("/api/crypto").get_json()
    assert portfolio["price_source"] == "CoinGecko"
    assert portfolio["market"]["BTC"]["quantity_units"] == 10_000_000
    history = auth_client.get(f"/api/accounts/{account_id}/history").get_json()
    receipt = auth_client.get(
        f"/api/transactions/{history['transactions'][0]['id']}/receipt"
    ).get_json()["receipt"]
    assert receipt["receipt_details"]["price_usd"] == "65000"

    sold = auth_client.post(
        "/api/crypto/sell",
        json={"symbol": "BTC", "account_id": account_id, "quantity_units": 4_000_000},
    )
    assert sold.status_code == 201
    assert sold.get_json()["amount_cents"] == 260_000
    assert auth_client.get(f"/api/accounts/{account_id}").get_json()["account"]["balance_cents"] == 610_000
    assert auth_client.get("/api/crypto").get_json()["market"]["BTC"]["quantity_units"] == 6_000_000


def test_crypto_rejects_bad_currency_funds_and_ownership(auth_client, client, monkeypatch):
    monkeypatch.setattr(
        "zenithpay.crypto.fetch_market_prices",
        lambda: {"BTC": Decimal("65000"), "ETH": Decimal("3500"), "SOL": Decimal("150")},
    )
    gbp = open_account(auth_client, "checking", "GB", "Pound account")
    response = auth_client.post(
        "/api/crypto/buy",
        json={"symbol": "ETH", "account_id": gbp, "amount_cents": 10_000},
    )
    assert response.get_json()["code"] == "currency_mismatch"

    usd = open_account(auth_client, "checking", "US", "USD account")
    broke = auth_client.post(
        "/api/crypto/buy",
        json={"symbol": "ETH", "account_id": usd, "amount_cents": 10_000},
    )
    assert broke.get_json()["code"] == "insufficient_funds"
    assert auth_client.get("/api/crypto").get_json()["market"]["ETH"]["quantity_units"] == 0

    client.post(
        "/api/auth/register",
        json={"name": "Other member", "email": "other@zenith.test", "password": "password123"},
    )
    foreign = client.post(
        "/api/crypto/buy",
        json={"symbol": "ETH", "account_id": usd, "amount_cents": 10_000},
    )
    assert foreign.status_code == 404
    assert foreign.get_json()["code"] == "account_not_found"


def test_crypto_rejects_invalid_symbol_and_insufficient_coin(auth_client, monkeypatch):
    monkeypatch.setattr(
        "zenithpay.crypto.fetch_market_prices",
        lambda: {"BTC": Decimal("65000"), "ETH": Decimal("3500"), "SOL": Decimal("150")},
    )
    usd = open_account(auth_client, "checking", "US", "USD account")
    bad_symbol = auth_client.post(
        "/api/crypto/buy",
        json={"symbol": "DOGE", "account_id": usd, "amount_cents": 10_000},
    )
    assert bad_symbol.get_json()["code"] == "invalid_symbol"
    no_coin = auth_client.post(
        "/api/crypto/sell",
        json={"symbol": "SOL", "account_id": usd, "quantity_units": 1_000_000},
    )
    assert no_coin.get_json()["code"] == "insufficient_coin"


def test_crypto_api_requires_auth(client):
    response = client.get("/api/crypto")
    assert response.status_code == 401
    assert response.get_json()["code"] == "auth_required"


def test_market_endpoint_uses_latest_provider_prices(auth_client, monkeypatch):
    monkeypatch.setattr(
        "zenithpay.crypto.fetch_market_prices",
        lambda: {"BTC": Decimal("81234.567"), "ETH": Decimal("4300.25"), "SOL": Decimal("201.5")},
    )
    market = auth_client.get("/api/crypto").get_json()
    assert market["market"]["BTC"]["price_usd"] == "81234.567"
    assert market["market"]["ETH"]["price_usd"] == "4300.25"
    assert market["prices_updated_at"]


def test_price_provider_parser_requests_and_parses_usd_quotes(auth_client, monkeypatch):
    import json

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "bitcoin": {"usd": 72000.125},
                    "ethereum": {"usd": 3800.5},
                    "solana": {"usd": 175.25},
                }
            ).encode()

    seen = {}

    def open_url(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr("zenithpay.crypto.urllib.request.urlopen", open_url)
    with auth_client.application.app_context():
        from zenithpay.crypto import fetch_market_prices

        prices = fetch_market_prices()
    assert prices == {
        "BTC": Decimal("72000.125"),
        "ETH": Decimal("3800.5"),
        "SOL": Decimal("175.25"),
    }
    assert "api.coingecko.com/api/v3/simple/price" in seen["url"]
    assert "vs_currencies=usd" in seen["url"]
    assert seen["timeout"] == 5


def test_live_market_failure_blocks_trades_without_fallback(auth_client, monkeypatch):
    account = open_account(auth_client, "checking", "US", "USD checking")
    fund(auth_client, account, 200_000)

    def unavailable():
        from zenithpay.crypto import MarketPriceError

        raise MarketPriceError("provider unavailable")

    monkeypatch.setattr("zenithpay.crypto.fetch_market_prices", unavailable)
    response = auth_client.get("/api/crypto")
    assert response.status_code == 502
    assert response.get_json()["code"] == "market_unavailable"

    trade = auth_client.post(
        "/api/crypto/buy",
        json={"symbol": "BTC", "account_id": account, "amount_cents": 10_000},
    )
    assert trade.status_code == 502
    assert auth_client.get(f"/api/accounts/{account}").get_json()["account"]["balance_cents"] == 200_000
