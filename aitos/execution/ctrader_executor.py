"""cTrader Open API execution adapter.

Bridges Spotware's official Twisted OpenApiPy SDK into AITOS' asyncio
OrderExecutor contract. No UI/browser automation is used.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Any

from aitos.execution.order_executor import OrderExecutor, OrderRequest, OrderResult
from aitos.models.trade import TradeSide

try:
    from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAAccountAuthReq,
        ProtoOAAccountAuthRes,
        ProtoOAApplicationAuthReq,
        ProtoOAApplicationAuthRes,
        ProtoOAExecutionEvent,
        ProtoOANewOrderReq,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType,
        ProtoOATradeSide,
    )
    from twisted.internet import reactor
except ImportError:  # pragma: no cover - optional integration dependency
    Client = EndPoints = Protobuf = TcpProtocol = None  # type: ignore[assignment]
    ProtoOAAccountAuthReq = ProtoOAAccountAuthRes = None  # type: ignore[assignment]
    ProtoOAApplicationAuthReq = ProtoOAApplicationAuthRes = None  # type: ignore[assignment]
    ProtoOAExecutionEvent = ProtoOANewOrderReq = None  # type: ignore[assignment]
    ProtoOAOrderType = ProtoOATradeSide = None  # type: ignore[assignment]
    reactor = None


class CTraderConfigurationError(RuntimeError):
    """Raised when cTrader integration is not installed/configured."""


class CTraderOrderExecutor(OrderExecutor):
    """Execute market/limit/stop orders through cTrader Open API.

    cTrader protocol volume is expressed in 0.01 units, so AITOS quantity is
    multiplied by 100. ``symbol_ids`` maps AITOS symbols to broker-specific
    numeric cTrader symbol IDs.

    The adapter is intentionally created from an already-running asyncio
    context. The SDK's Twisted reactor is isolated in its own daemon thread;
    AITOS never starts/stops the global reactor from individual orders.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        access_token: str,
        account_id: int,
        symbol_ids: dict[str, int],
        live: bool = False,
        request_timeout: float = 15.0,
    ) -> None:
        if Client is None or reactor is None:
            raise CTraderConfigurationError(
                "install 'ctrader-open-api' to enable cTrader execution"
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._account_id = int(account_id)
        self._symbol_ids = {k.upper(): int(v) for k, v in symbol_ids.items()}
        self._timeout = request_timeout
        self._endpoint = (
            EndPoints.PROTOBUF_LIVE_HOST if live else EndPoints.PROTOBUF_DEMO_HOST
        )
        self._client = Client(self._endpoint, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._connected = threading.Event()
        self._authenticated = threading.Event()
        self._reactor_started = False
        self._start_runtime()

    def _start_runtime(self) -> None:
        self._client.setConnectedCallback(self._on_connected)
        self._client.setDisconnectedCallback(self._on_disconnected)
        self._client.setMessageReceivedCallback(self._on_message)
        if not self._reactor_started:
            self._reactor_started = True
            threading.Thread(
                target=reactor.run,
                kwargs={"installSignalHandlers": False},
                daemon=True,
                name="aitos-ctrader-reactor",
            ).start()
        reactor.callFromThread(self._client.connect)

    def _on_connected(self, client: Any) -> None:
        self._connected.set()
        request = ProtoOAApplicationAuthReq()
        request.clientId = self._client_id
        request.clientSecret = self._client_secret
        client.send(request)

    def _on_disconnected(self, client: Any, reason: Any) -> None:
        self._connected.clear()
        self._authenticated.clear()

    def _on_message(self, client: Any, message: Any) -> None:
        if message.payloadType == ProtoOAApplicationAuthRes().payloadType:
            request = ProtoOAAccountAuthReq()
            request.ctidTraderAccountId = self._account_id
            request.accessToken = self._access_token
            client.send(request)
        elif message.payloadType == ProtoOAAccountAuthRes().payloadType:
            self._authenticated.set()

    async def _send(self, request: Any) -> Any:
        """Bridge a Twisted Deferred-returning SDK call into asyncio."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def on_success(message: Any) -> Any:
            loop.call_soon_threadsafe(_set_result, message)
            return message

        def on_error(failure: Any) -> Any:
            error = RuntimeError(str(getattr(failure, "value", failure)))
            loop.call_soon_threadsafe(_set_error, error)
            return failure

        def _set_result(message: Any) -> None:
            if not future.done():
                future.set_result(message)

        def _set_error(error: Exception) -> None:
            if not future.done():
                future.set_exception(error)

        def dispatch() -> None:
            deferred = self._client.send(request)
            deferred.addCallback(on_success)
            deferred.addErrback(on_error)

        reactor.callFromThread(dispatch)
        return await asyncio.wait_for(future, timeout=self._timeout)

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        if request.order_type not in {"MARKET", "LIMIT", "STOP"}:
            raise ValueError(f"unsupported cTrader order type: {request.order_type}")
        symbol_id = self._symbol_ids.get(request.symbol.upper())
        if symbol_id is None:
            raise ValueError(f"missing cTrader symbol id for {request.symbol}")
        if not self._connected.wait(self._timeout):
            raise RuntimeError("cTrader connection timeout")
        if not self._authenticated.wait(self._timeout):
            raise RuntimeError("cTrader account authentication timeout")

        order = ProtoOANewOrderReq()
        order.ctidTraderAccountId = self._account_id
        order.symbolId = symbol_id
        order.orderType = ProtoOAOrderType.Value(request.order_type)
        order.tradeSide = ProtoOATradeSide.Value(
            "BUY" if request.side is TradeSide.LONG else "SELL"
        )
        order.volume = max(1, round(request.quantity * 100))
        if request.order_type == "LIMIT":
            order.limitPrice = request.reference_price
        elif request.order_type == "STOP":
            order.stopPrice = request.reference_price

        message = await self._send(order)
        if message.payloadType != ProtoOAExecutionEvent().payloadType:
            raise RuntimeError("cTrader returned a non-execution response to new order")
        event = Protobuf.extract(message)
        order_result = getattr(event, "order", None)
        if order_result is None:
            raise RuntimeError("cTrader execution response did not contain an order")
        execution_price = float(getattr(order_result, "executionPrice", 0.0))
        executed_volume = float(getattr(order_result, "executedVolume", 0)) / 100.0
        return OrderResult(
            order_id=str(getattr(order_result, "orderId", "")),
            symbol=request.symbol,
            side=request.side,
            filled_quantity=executed_volume,
            fill_price=execution_price,
            filled_at=datetime.now(timezone.utc).isoformat(),
            success=True,
        )

    @property
    def supports_exchange_side_stops(self) -> bool:
        # The generic AITOS stop API is not yet mapped to cTrader's dedicated
        # protection/modify messages. Keep this false rather than claiming a
        # capability that the routed executor cannot safely guarantee.
        return False
