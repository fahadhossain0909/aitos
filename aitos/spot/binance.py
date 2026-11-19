"""Async Binance Spot REST adapter (public market data + signed account/order API)."""
from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import aiohttp

from aitos.exchange.rate_limiter import TokenBucketRateLimiter

from aitos.spot.filters import D, SpotSymbolFilters, parse_spot_exchange_info
from aitos.spot.models import (SpotBalance, SpotFill, SpotInstrument, SpotOrder,
    SpotOrderRequest, SpotOrderSide, SpotOrderStatus)


class BinanceSpotAdapter:
    MAINNET = "https://api.binance.com"
    TESTNET = "https://testnet.binance.vision"

    def __init__(self, *, api_key: str | None = None, api_secret: str | None = None,
                 testnet: bool = True, session: aiohttp.ClientSession | None = None,
                 timeout_seconds: float = 10.0,
                 rate_limiter: TokenBucketRateLimiter | None = None) -> None:
        self.api_key, self.api_secret = api_key, api_secret
        self.testnet = testnet
        self.base_url = self.TESTNET if testnet else self.MAINNET
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.filters: dict[str, SpotSymbolFilters] = {}
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(
            capacity=1200, refill_per_second=20.0, reserved_capacity=100
        )
        self._server_time_offset_ms = 0

    async def __aenter__(self) -> "BinanceSpotAdapter":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    def _signed_params(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Binance Spot API credentials are required")
        values = dict(params)
        values.setdefault("recvWindow", 5000)
        values["timestamp"] = int(time.time() * 1000) + self._server_time_offset_ms
        query = urlencode(values, doseq=True)
        values["signature"] = hmac.new(self.api_secret.encode(), query.encode(),
                                       hashlib.sha256).hexdigest()
        return values

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                       signed: bool = False, api_key_only: bool = False,
                       weight: int = 1) -> Any:
        await self._rate_limiter.acquire(weight)
        await self.connect()
        assert self._session is not None
        headers = {}
        values = dict(params or {})
        if signed:
            values = self._signed_params(values)
            headers["X-MBX-APIKEY"] = self.api_key or ""
        elif api_key_only:
            if not self.api_key:
                raise RuntimeError("Binance Spot API key is required")
            headers["X-MBX-APIKEY"] = self.api_key
        async with self._session.request(method, self.base_url + path,
                                         params=values, headers=headers) as response:
            payload = await response.json(content_type=None)
            if response.status >= 400:
                code = payload.get("code") if isinstance(payload, dict) else None
                message = payload.get("msg", "Binance API error") if isinstance(payload, dict) else str(payload)
                raise RuntimeError(f"Binance Spot HTTP {response.status} ({code}): {message}")
            return payload

    async def sync_server_time(self) -> int:
        """Synchronize signed-request timestamps against Binance server time."""
        before = int(time.time() * 1000)
        raw = await self._request("GET", "/api/v3/time")
        after = int(time.time() * 1000)
        midpoint = (before + after) // 2
        self._server_time_offset_ms = int(raw["serverTime"]) - midpoint
        return self._server_time_offset_ms

    @property
    def rate_limit_snapshot(self) -> dict[str, Any]:
        return self._rate_limiter.snapshot()

    async def exchange_info(self) -> dict[str, SpotSymbolFilters]:
        raw = await self._request("GET", "/api/v3/exchangeInfo", weight=20)
        self.filters = parse_spot_exchange_info(raw)
        return dict(self.filters)

    async def instruments(self, quote_asset: str = "USDT") -> list[SpotInstrument]:
        if not self.filters:
            await self.exchange_info()
        return [SpotInstrument(
            symbol=f.symbol, base_asset=f.base_asset, quote_asset=f.quote_asset,
            status=f.status, is_spot_trading_allowed=f.is_spot_trading_allowed,
        ) for f in self.filters.values()
            if f.quote_asset == quote_asset.upper() and f.status == "TRADING"
            and f.is_spot_trading_allowed]

    async def discover_universe(self, *, quote_asset: str = "USDT",
                                min_quote_volume: Decimal = Decimal("100000"),
                                limit: int = 20) -> list[SpotInstrument]:
        """Select active Spot instruments by 24h quote turnover, never by symbol guess."""
        if not 1 <= limit <= 100:
            raise ValueError("universe limit must be between 1 and 100")
        if min_quote_volume < 0:
            raise ValueError("minimum quote volume cannot be negative")
        if not self.filters:
            await self.exchange_info()
        tickers = await self.ticker_24h()
        by_symbol = {
            str(row.get("symbol", "")).upper(): row
            for row in tickers if isinstance(row, dict)
        }
        candidates = []
        for symbol, filters in self.filters.items():
            if (filters.quote_asset != quote_asset.upper()
                    or filters.status != "TRADING"
                    or not filters.is_spot_trading_allowed):
                continue
            ticker = by_symbol.get(symbol)
            if ticker is None:
                continue
            quote_volume = D(ticker.get("quoteVolume", "0"))
            if quote_volume >= min_quote_volume:
                candidates.append((quote_volume, SpotInstrument(
                    symbol=symbol, base_asset=filters.base_asset,
                    quote_asset=filters.quote_asset, status=filters.status,
                    is_spot_trading_allowed=True,
                )))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [instrument for _, instrument in candidates[:limit]]

    async def klines(self, symbol: str, interval: str, limit: int = 500) -> list[list[Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await self._request("GET", "/api/v3/klines",
                                   params={"symbol": symbol.upper(), "interval": interval, "limit": limit}, weight=2)

    async def depth(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        if limit not in {5, 10, 20, 50, 100, 500, 1000, 5000}:
            raise ValueError("unsupported Binance depth limit")
        return await self._request("GET", "/api/v3/depth",
                                   params={"symbol": symbol.upper(), "limit": limit}, weight=(5 if limit <= 100 else (25 if limit <= 500 else (50 if limit <= 1000 else 250))))

    async def ticker_24h(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol.upper()} if symbol else {}
        return await self._request("GET", "/api/v3/ticker/24hr", params=params, weight=2 if symbol else 80)

    async def create_listen_key(self) -> str:
        """Create a private User Data Stream listen key."""
        raw = await self._request("POST", "/api/v3/userDataStream", api_key_only=True)
        return str(raw["listenKey"])

    async def keepalive_listen_key(self, listen_key: str) -> None:
        await self._request("PUT", "/api/v3/userDataStream",
                            params={"listenKey": listen_key}, api_key_only=True)

    async def close_listen_key(self, listen_key: str) -> None:
        await self._request("DELETE", "/api/v3/userDataStream",
                            params={"listenKey": listen_key}, api_key_only=True)

    async def account(self) -> list[SpotBalance]:
        raw = await self._request("GET", "/api/v3/account", signed=True, weight=20)
        return [SpotBalance(x["asset"], D(x["free"]), D(x["locked"]))
                for x in raw.get("balances", [])]

    async def place_order(self, request: SpotOrderRequest) -> SpotOrder:
        params: dict[str, Any] = {
            "symbol": request.symbol, "side": request.side.value,
            "type": request.order_type.value,
            "newOrderRespType": "FULL",
        }
        if request.quantity is not None:
            params["quantity"] = format(request.quantity, "f")
        if request.quote_order_quantity is not None:
            params["quoteOrderQty"] = format(request.quote_order_quantity, "f")
        if request.price is not None:
            params["price"] = format(request.price, "f")
        if request.time_in_force:
            params["timeInForce"] = request.time_in_force
        if request.client_order_id:
            params["newClientOrderId"] = request.client_order_id
        raw = await self._request("POST", "/api/v3/order", params=params, signed=True, weight=1)
        return self._parse_order(raw, request.side)

    async def query_order(self, symbol: str, *, order_id: int | None = None,
                          client_order_id: str | None = None) -> SpotOrder:
        if (order_id is None) == (client_order_id is None):
            raise ValueError("provide exactly one order_id or client_order_id")
        params: dict[str, Any] = {"symbol": symbol.upper()}
        params["orderId" if order_id is not None else "origClientOrderId"] = (
            order_id if order_id is not None else client_order_id)
        raw = await self._request("GET", "/api/v3/order", params=params, signed=True, weight=2)
        return self._parse_order(raw, SpotOrderSide(raw["side"]))

    async def my_trades(self, symbol: str, *, order_id: int | None = None,
                        from_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Return authenticated fills for durable inventory reconciliation."""
        if order_id is not None and from_id is not None:
            raise ValueError("order_id and from_id are mutually exclusive")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        params: dict[str, Any] = {"symbol": symbol.upper(), "limit": limit}
        if order_id is not None:
            params["orderId"] = order_id
        if from_id is not None:
            params["fromId"] = from_id
        return await self._request("GET", "/api/v3/myTrades", params=params, signed=True, weight=5)

    async def cancel_order(self, symbol: str, *, order_id: int | None = None,
                           client_order_id: str | None = None) -> dict[str, Any]:
        if (order_id is None) == (client_order_id is None):
            raise ValueError("provide exactly one order_id or client_order_id")
        params: dict[str, Any] = {"symbol": symbol.upper()}
        params["orderId" if order_id is not None else "origClientOrderId"] = (
            order_id if order_id is not None else client_order_id)
        return await self._request("DELETE", "/api/v3/order", params=params, signed=True, weight=1)

    @staticmethod
    def _parse_order(raw: dict[str, Any], side: SpotOrderSide) -> SpotOrder:
        fills = tuple(SpotFill(D(f["price"]), D(f["qty"]), D(f.get("commission", 0)),
                               f.get("commissionAsset", ""), int(f["tradeId"]) if f.get("tradeId") is not None else None)
                      for f in raw.get("fills", []))
        return SpotOrder(
            symbol=raw["symbol"], order_id=int(raw["orderId"]),
            client_order_id=raw.get("clientOrderId", ""),
            side=side, status=SpotOrderStatus(raw.get("status", "UNKNOWN")) if raw.get("status") in SpotOrderStatus._value2member_map_ else SpotOrderStatus.UNKNOWN,
            executed_quantity=D(raw.get("executedQty", 0)),
            cumulative_quote_quantity=D(raw.get("cummulativeQuoteQty", 0)),
            fills=fills, raw=raw,
        )
