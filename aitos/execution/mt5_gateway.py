"""Linux-side client for the isolated Windows MetaTrader 5 gateway.

The AITOS core stays platform-neutral. A small Windows process running the
official MetaTrader5 Python package owns the terminal connection and exposes
only authenticated JSON RPC to this client.
"""

from __future__ import annotations

import aiohttp

from aitos.execution.order_executor import OrderExecutor, OrderRequest, OrderResult


class MT5GatewayError(RuntimeError):
    pass


class MT5GatewayOrderExecutor(OrderExecutor):
    def __init__(
        self,
        base_url: str,
        auth_token: str,
        timeout: float = 10.0,
        session_factory=aiohttp.ClientSession,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._timeout = timeout
        self._session_factory = session_factory

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        payload = {
            "symbol": request.symbol,
            "side": request.side.value,
            "quantity": request.quantity,
            "reference_price": request.reference_price,
            "order_type": request.order_type,
            "client_order_id": request.client_order_id,
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with self._session_factory(timeout=timeout) as session:
            async with session.post(
                f"{self._base_url}/v1/orders",
                json=payload,
                headers={"Authorization": f"Bearer {self._auth_token}"},
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise MT5GatewayError(f"MT5 gateway {response.status}: {body}")
                return OrderResult(
                    order_id=str(body["order_id"]),
                    symbol=request.symbol,
                    side=request.side,
                    filled_quantity=float(body["filled_quantity"]),
                    fill_price=float(body["fill_price"]),
                    success=bool(body.get("success", True)),
                    error=body.get("error"),
                )

    async def account(self) -> dict:
        return await self._get("/v1/account")

    async def positions(self) -> list[dict]:
        return await self._get("/v1/positions")

    async def health(self) -> dict:
        return await self._get("/health")

    async def _get(self, path: str):
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with self._session_factory(timeout=timeout) as session:
            async with session.get(
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._auth_token}"},
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise MT5GatewayError(f"MT5 gateway {response.status}: {body}")
                return body
