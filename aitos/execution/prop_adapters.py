"""Prop-firm execution adapters built on the existing OrderExecutor contract.

Only adapters with a documented, programmatic trading API belong here. UI
automation is deliberately unsupported.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from aitos.execution.binance_executor import BinanceFuturesOrderExecutor
from aitos.execution.order_executor import OrderRequest, OrderResult


class AlfaxOrderExecutor(BinanceFuturesOrderExecutor):
    """Alfax adapter using its Binance Futures-compatible API.

    Alfax documents Binance-compatible paths, HMAC authentication and
    WebSocket formats. Reusing the hardened Binance executor keeps signing,
    idempotency, precision and reduce-only behavior in one implementation.
    """

    def __init__(self, api_key: str, api_secret: str, **kwargs: Any) -> None:
        super().__init__(api_key, api_secret, **kwargs)
        self._base_url = kwargs.pop("base_url", "https://api.alfax.trade")


class TradeLockerOrderExecutor:
    """Minimal TradeLocker REST executor.

    TradeLocker requires JWT authentication plus account number and route
    metadata. Instrument/route identifiers are therefore explicit instead of
    being inferred from a symbol string.
    """

    def __init__(
        self,
        email: str,
        password: str,
        server: str,
        account_id: int,
        acc_num: int,
        route_id: int,
        instrument_id: int,
        base_url: str = "https://live.tradelocker.com/backend-api",
        session_factory: Any = aiohttp.ClientSession,
    ) -> None:
        self._email = email
        self._password = password
        self._server = server
        self._account_id = account_id
        self._acc_num = acc_num
        self._route_id = route_id
        self._instrument_id = instrument_id
        self._base_url = base_url.rstrip("/")
        self._session_factory = session_factory
        self._session: aiohttp.ClientSession | None = None
        self._access_token: str | None = None

    async def connect(self) -> None:
        self._session = self._session_factory()
        response = await self._request(
            "POST",
            "/auth/jwt/token",
            json={"email": self._email, "password": self._password, "server": self._server},
            auth=False,
        )
        self._access_token = response["accessToken"]

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        if self._access_token is None:
            raise RuntimeError("TradeLockerOrderExecutor.connect() must be called first")
        side = 0 if request.side.value == "LONG" else 1
        order_type = "market" if request.order_type == "MARKET" else request.order_type.lower()
        payload = {
            "qty": request.quantity,
            "routeId": self._route_id,
            "side": side,
            "validity": "IOC" if order_type == "market" else "GTC",
            "type": order_type,
            "tradableInstrumentId": self._instrument_id,
            "price": 0 if order_type == "market" else request.reference_price,
        }
        if request.client_order_id:
            payload["customTag"] = request.client_order_id
        response = await self._request(
            "POST",
            f"/trade/accounts/{self._account_id}/orders",
            json=payload,
        )
        order_id = str(response.get("orderId", response.get("id", "")))
        return OrderResult(
            order_id=order_id,
            symbol=request.symbol,
            side=request.side,
            filled_quantity=request.quantity if response else 0.0,
            fill_price=request.reference_price,
            success=True,
        )

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None, auth: bool = True
    ) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("connect() must be called first")
        headers = {"Content-Type": "application/json"}
        if auth:
            if self._access_token is None:
                raise RuntimeError("TradeLocker access token is missing")
            headers["Authorization"] = f"Bearer {self._access_token}"
            headers["accNum"] = str(self._acc_num)
        async with self._session.request(
            method, f"{self._base_url}{path}", json=json, headers=headers
        ) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"TradeLocker API {response.status}: {body}")
            return body if isinstance(body, dict) else {"data": body}
