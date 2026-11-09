"""Cross-venue order execution attribution decorator."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aitos.execution.order_executor import OrderExecutor, OrderRequest, OrderResult
from aitos.models.trade import TradeSide

if TYPE_CHECKING:
    from aitos.statistics.decision_ledger import DecisionOutcomeLedger


@dataclass(frozen=True)
class OrderAttributionContext:
    decision_id: str
    entity_id: str
    reason: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.decision_id.strip() or not self.entity_id.strip():
            raise ValueError("decision_id and entity_id are required")
        if not self.reason.strip():
            raise ValueError("attribution reason is required")


class AttributionPersistenceError(RuntimeError):
    """The venue may have accepted an order but its ledger write failed.

    The original result is attached so callers reconcile by venue order ID and
    do not blindly resubmit an order whose execution state is uncertain.
    """

    def __init__(self, message: str, order_result: OrderResult) -> None:
        super().__init__(message)
        self.order_result = order_result
        self.may_have_executed = True


class AttributedOrderExecutor(OrderExecutor):
    """Decorator that records order decisions for any OrderExecutor adapter.

    The context resolver binds each request to its scanner/position trace.
    The same decorator can wrap simulated, Binance, cTrader, MT5 and other
    adapters without changing venue-specific request handling.
    """

    def __init__(
        self,
        delegate: OrderExecutor,
        ledger: DecisionOutcomeLedger,
        context_resolver: Callable[[OrderRequest], OrderAttributionContext],
    ) -> None:
        self.delegate = delegate
        self.ledger = ledger
        self.context_resolver = context_resolver
        self._order_contexts: dict[
            str, tuple[OrderRequest, OrderAttributionContext]
        ] = {}

    @property
    def supports_exchange_side_stops(self) -> bool:
        return self.delegate.supports_exchange_side_stops

    def _record(
        self,
        context: OrderAttributionContext,
        *,
        symbol: str,
        side: TradeSide,
        decision_type: str,
        accepted: bool,
        payload: Mapping[str, Any],
        decision_id: str | None = None,
    ) -> None:
        self.ledger.record_decision(
            decision_id or context.decision_id,
            entity_id=context.entity_id,
            symbol=symbol,
            side=side.value if hasattr(side, "value") else str(side),
            decision_type=decision_type,
            accepted=accepted,
            reason=context.reason,
            payload={**dict(context.payload), **dict(payload)},
        )

    def _record_result(
        self,
        request: OrderRequest,
        context: OrderAttributionContext,
        result: OrderResult,
        *,
        decision_type: str | None = None,
    ) -> None:
        kind = decision_type or ("EXIT" if request.reduce_only else "ENTRY")
        payload = {
            "client_order_id": request.client_order_id,
            "venue_order_id": result.order_id,
            "order_type": request.order_type,
            "requested_quantity": request.quantity,
            "filled_quantity": result.filled_quantity,
            "fill_price": result.fill_price,
            "reference_price": request.reference_price,
            "reduce_only": request.reduce_only,
            "execution_success": result.success,
            "execution_error": result.error,
            "filled_at": result.filled_at,
        }
        self._record(
            context,
            symbol=request.symbol,
            side=request.side,
            decision_type=kind,
            accepted=result.success or result.filled_quantity > 0.0,
            payload=payload,
        )
        if not result.success or result.error:
            self._record(
                context,
                symbol=request.symbol,
                side=request.side,
                decision_type="EXECUTION_FAILURE",
                accepted=False,
                payload=payload,
                decision_id=f"{context.decision_id}:execution-failure",
            )
        self._order_contexts[result.order_id] = (request, context)

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        context = self.context_resolver(request)
        try:
            result = await self.delegate.submit_order(request)
        except Exception as exc:
            failure = OrderResult(
                order_id=request.client_order_id or "unknown",
                symbol=request.symbol,
                side=request.side,
                filled_quantity=0.0,
                fill_price=0.0,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
                self._record_result(request, context, failure)
            except Exception as ledger_error:
                raise AttributionPersistenceError(
                    f"execution and attribution persistence both failed: {ledger_error}",
                    failure,
                ) from exc
            raise
        try:
            self._record_result(request, context, result)
        except Exception as exc:
            raise AttributionPersistenceError(
                f"venue result {result.order_id} could not be durably attributed: {exc}",
                result,
            ) from exc
        return result

    async def get_order_status(self, symbol: str, order_id: str) -> dict | None:
        return await self.delegate.get_order_status(symbol, order_id)

    async def place_stop_loss_order(
        self, symbol: str, side: TradeSide, quantity: float, stop_price: float
    ) -> OrderResult:
        request = OrderRequest(
            symbol=symbol,
            side=side,
            quantity=quantity,
            reference_price=stop_price,
            order_type="STOP_MARKET",
            reduce_only=True,
        )
        context = self.context_resolver(request)
        result = await self.delegate.place_stop_loss_order(
            symbol, side, quantity, stop_price
        )
        try:
            self._record_result(request, context, result, decision_type="MANAGE")
        except Exception as exc:
            raise AttributionPersistenceError(
                f"stop order result {result.order_id} could not be attributed: {exc}",
                result,
            ) from exc
        return result

    async def place_take_profit_order(
        self, symbol: str, side: TradeSide, quantity: float, take_profit_price: float
    ) -> OrderResult:
        request = OrderRequest(
            symbol=symbol,
            side=side,
            quantity=quantity,
            reference_price=take_profit_price,
            order_type="TAKE_PROFIT_MARKET",
            reduce_only=True,
        )
        context = self.context_resolver(request)
        result = await self.delegate.place_take_profit_order(
            symbol, side, quantity, take_profit_price
        )
        try:
            self._record_result(request, context, result, decision_type="MANAGE")
        except Exception as exc:
            raise AttributionPersistenceError(
                f"take-profit order result {result.order_id} could not be attributed: {exc}",
                result,
            ) from exc
        return result

    async def cancel_resting_order(self, symbol: str, order_id: str) -> None:
        context_entry = self._order_contexts.get(order_id)
        try:
            await self.delegate.cancel_resting_order(symbol, order_id)
        except Exception as exc:
            if context_entry is not None:
                request, context = context_entry
                failure = OrderResult(
                    order_id=order_id,
                    symbol=symbol,
                    side=request.side,
                    filled_quantity=0.0,
                    fill_price=request.reference_price,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                try:
                    self._record_result(
                        request, context, failure, decision_type="MANAGE"
                    )
                except Exception as ledger_error:
                    raise AttributionPersistenceError(
                        f"cancel failed and its attribution could not be persisted: {ledger_error}",
                        failure,
                    ) from exc
            raise
        if context_entry is not None:
            request, context = context_entry
            try:
                self._record(
                    context,
                    symbol=symbol,
                    side=request.side,
                    decision_type="MANAGE",
                    accepted=True,
                    payload={"action": "CANCEL", "venue_order_id": order_id},
                    decision_id=f"{context.decision_id}:cancel",
                )
            except Exception as exc:
                result = OrderResult(
                    order_id=order_id,
                    symbol=symbol,
                    side=request.side,
                    filled_quantity=0.0,
                    fill_price=request.reference_price,
                    success=True,
                )
                raise AttributionPersistenceError(
                    f"cancel of {order_id} succeeded but attribution persistence failed: {exc}",
                    result,
                ) from exc

    async def get_resting_order_status(self, symbol: str, order_id: str) -> str | None:
        return await self.delegate.get_resting_order_status(symbol, order_id)
