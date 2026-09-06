"""cTrader Open API execution adapter.

The adapter bridges cTrader's official Twisted SDK into AITOS' asyncio
OrderExecutor contract. It uses the official OpenApiPy package rather than
browser/UI automation. OAuth access tokens are supplied by the caller; token
refresh is handled by the separate auth helper in ``aitos/execution/ctrader_auth.py``.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

from aitos.execution.order_executor import OrderExecutor, OrderRequest, OrderResult

try:
    from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
    from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAAccountAuthReq,
        ProtoOAApplicationAuthReq,
        ProtoOAExecutionEvent,
        ProtoOANewOrderReq,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType,
        ProtoOATradeSide,
    )
    from twisted.internet import reactor
except ImportError:  # pragma: no cover - exercised only without optional dependency
    Client = EndPoints = Protobuf = TcpProtocol = None  # type: ignore[assignment]
    ProtoHeartbeatEvent = ProtoOAAccountAuthReq = ProtoOAApplicationAuthReq = None  # type: ignore[assignment]
    ProtoOAExecutionEvent = ProtoOANewOrderReq = None  # type: ignore[assignment]
    ProtoOAOrderType = ProtoOATradeSide = None  # type: ignore[assignment]
    reactor = None


class CTraderConfigurationError(RuntimeError):
    pass


class CTraderOrderExecutor(OrderExecutor):
    """Execute AITOS orders through cTrader Open API.

    ``symbol_id`` must be the broker's cTrader numeric symbol id. Volume is
    converted to cTrader's protocol units (0.01 of a unit) as required by
    ProtoOANewOrderReq. The adapter waits for the execution event instead of
    treating request acceptance as a fill.
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
                "cTrader support requires the optional 'ctrader-open-api' package"
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._account_id = int(account_id)
        self._symbol_ids = {k.upper(): int(v) for k, v in symbol_ids.items()}
        self._request_timeout = request_timeout
        self._endpoint = (
            EndPoints.PROTOBUF_LIVE_HOST if live else EndPoints.PROTOBUF_DEMO_HOST
        )
        self._client = Client(self._endpoint, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._connected = threading.Event()
        self._authenticated = threading.Event()
        self._reactor_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, asyncio.Future[OrderResult]] = {}
        self._lock = threading.Lock()
        self._start_runtime()

    def _start_runtime(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._client.setConnectedCallback(self._on_connected)
        self._client.setDisconnectedCallback(self._on_disconnected)
        self._client.setMessageReceivedCallback(self._on_message)
        self._reactor_thread = threading.Thread(
            target=reactor.run, kwargs={"installSignalHandlers": False}, daemon=True
        )
        self._reactor_thread.start()
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
        if message.payloadType == ProtoOAExecutionEvent().payloadType:
            event = Protobuf.extract(message)
            order = getattr(event, "order", None)
            if order is None:
                return
            order_id = str(getattr(order, "orderId", ""))
            with self._lock:
                future = self._pending.get(order_id)
            if future is None or self._loop is None or future.done():
                return
            filled = float(getattr(order, "executedVolume", 0)) / 100.0
            price = float(getattr(order, "executionPrice", 0.0))
            result = OrderResult(
                order_id=order_id,
                symbol="",
                side=__import__("aitos.models.trade", fromlist=["TradeSide"]).TradeSide.LONG,
                filled_quantity=filled,
                fill_price=price,
                success=True,
            )
            self._loop.call_soon_threadsafe(future.set_result, result)

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        if request.order_type not in {"MARKET", "LIMIT", "STOP"}:
            raise ValueError(f"unsupported cTrader order type: {request.order_type}")
        if request.symbol.upper() not in self._symbol_ids:
            raise ValueError(f"missing cTrader symbol id for {request.symbol}")
        if not self._connected.wait(self._request_timeout):
            raise RuntimeError("cTrader connection timeout")
        symbol_id = self._symbol_ids[request.symbol.upper()]
        order = ProtoOANewOrderReq()
        order.ctidTraderAccountId = self._account_id
        order.symbolId = symbol_id
        order.orderType = ProtoOAOrderType.Value(request.order_type)
        order.tradeSide = ProtoOATradeSide.Value(
            "BUY" if request.side.value == "LONG" else "SELL"
        )
        order.volume = max(1, round(request.quantity * 100))
        if request.order_type == "LIMIT":
            order.limitPrice = request.reference_price
        elif request.order_type == "STOP":
            order.stopPrice = request.reference_price

        # cTrader returns the definitive execution event asynchronously. The
        # SDK's request Deferred is deliberately not interpreted as a fill.
        future: asyncio.Future[OrderResult] = asyncio.get_running_loop().create_future()
        client_msg_id = request.client_order_id or uuid.uuid4().hex
        reactor.callFromThread(self._client.send, order, clientMsgId=client_msg_id)
        try:
            result = await asyncio.wait_for(future, timeout=self._request_timeout)
        finally:
            # Pending events are keyed by server order id; request id is not
            # guaranteed to equal the order id, so timeout cleanup is best-effort.
            pass
        return OrderResult(
            order_id=result.order_id,
            symbol=request.symbol,
            side=request.side,
            filled_quantity=result.filled_quantity,
            fill_price=result.fill_price,
            filled_at=result.filled_at,
            success=result.success,
            error=result.error,
        )

    @property
    def supports_exchange_side_stops(self) -> bool:
        return True
