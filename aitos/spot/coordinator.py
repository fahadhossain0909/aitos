"""Capital-gated Spot entry and inventory-bounded exit orchestration."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.intelligence.exit_intelligence.models import ExitAction, ExitDecision
from aitos.market_data.venues import InstrumentKey, MarketType, Venue
from aitos.models.trade import Opportunity, TradeSide
from aitos.spot.execution import SpotExecutionDisabled, SpotOrderExecutor
from aitos.spot.inventory import InventoryLedger
from aitos.spot.models import (
    SpotOrder,
    SpotOrderRequest,
    SpotOrderSide,
    SpotOrderStatus,
    SpotOrderType,
)


class SpotAuthorizationRejected(RuntimeError):
    """Raised when a Spot order violates strategy, capital or inventory policy."""


class SpotTradingCoordinator:
    def __init__(
        self,
        *,
        capital_gateway: CapitalGateway,
        executor: SpotOrderExecutor,
        inventory: InventoryLedger,
        fee_bps: float = 10.0,
        slippage_bps: float = 8.0,
    ) -> None:
        if fee_bps < 0 or slippage_bps < 0:
            raise ValueError("Spot costs cannot be negative")
        self.capital_gateway = capital_gateway
        self.executor = executor
        self.inventory = inventory
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps

    async def submit_entry(
        self,
        *,
        equity_quote: float,
        opportunity: Opportunity,
        request: SpotOrderRequest,
    ) -> SpotOrder:
        if not self.executor.enabled:
            raise SpotExecutionDisabled("Spot execution is disabled")
        if opportunity.side is not TradeSide.LONG:
            raise SpotAuthorizationRejected(
                "Spot entry is long-only; short exposure is unsupported"
            )
        if request.symbol != opportunity.symbol.upper():
            raise SpotAuthorizationRejected(
                "Spot order symbol must match the authorized opportunity"
            )
        if request.side is not SpotOrderSide.BUY:
            raise SpotAuthorizationRejected("Spot entry requires BUY")
        if request.order_type is not SpotOrderType.MARKET:
            raise SpotAuthorizationRejected("Spot entry requires a market BUY")
        if request.quote_order_quantity is None:
            raise SpotAuthorizationRejected(
                "Spot entry must be bounded by quoteOrderQty"
            )

        key = InstrumentKey(Venue.BINANCE, MarketType.SPOT, request.symbol)
        scoped_consensus = dict(opportunity.agent_consensus or {})
        scoped_consensus.update(
            {
                "leverage": 1.0,
                "liquidation_buffer_pct": None,
                "funding_bps": 0.0,
            }
        )
        scoped_opportunity = replace(
            opportunity, symbol=key.value, agent_consensus=scoped_consensus
        )
        decision, allocation = self.capital_gateway.authorize_opportunity(
            equity_quote,
            scoped_opportunity,
            fee_bps=self.fee_bps,
            slippage_bps=self.slippage_bps,
            funding_bps=0.0,
        )
        if not decision.eligible or allocation is None:
            reasons = ", ".join(decision.rejection_reasons)
            raise SpotAuthorizationRejected(
                f"capital gateway rejected Spot entry: {reasons}"
            )

        approved_budget = Decimal(str(allocation.capital_usd))
        if request.quote_order_quantity > approved_budget:
            self.capital_gateway.release(key.value)
            raise SpotAuthorizationRejected(
                "requested quote notional exceeds approved Spot allocation"
            )

        client_order_id = request.client_order_id or ("aitos-" + uuid.uuid4().hex[:28])
        request = replace(request, client_order_id=client_order_id)
        try:
            self.inventory.record_order_intent(
                client_order_id=client_order_id,
                symbol=request.symbol,
                side=request.side.value,
                order_type=request.order_type.value,
                quantity=request.quantity,
                quote_order_quantity=request.quote_order_quantity,
            )
        except Exception:
            self.capital_gateway.release(key.value)
            raise

        # Pin before network submission. A timeout is ambiguous; keep the
        # reservation until exchange query and fill reconciliation complete.
        if not self.capital_gateway.pin(key.value):
            self.inventory.update_order_intent(
                client_order_id, status=SpotOrderStatus.REJECTED.value
            )
            self.capital_gateway.release(key.value)
            raise SpotAuthorizationRejected("could not pin Spot capital reservation")

        try:
            order = await self.executor.submit(request)
        except Exception:
            self.inventory.update_order_intent(client_order_id, status="UNKNOWN")
            raise

        self.inventory.update_order_intent(
            client_order_id,
            status=order.status.value,
            exchange_order_id=order.order_id,
        )
        if self._definitively_unfilled(order):
            self.capital_gateway.release(key.value)
        return order

    async def submit_exit(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        authorization: ExitDecision,
        client_order_id: str | None = None,
    ) -> SpotOrder:
        """Submit an inventory-bounded Spot sell from a shared ExitDecision.

        Spot has no exchange reduceOnly flag. Only an explicit EIE EXIT or
        MANAGE decision can authorize a sell, and MANAGE is capped by its
        suggested reduction fraction.
        """
        if not self.executor.enabled:
            raise SpotExecutionDisabled("Spot execution is disabled")
        if authorization.symbol.upper() != symbol.upper():
            raise SpotAuthorizationRejected(
                "ExitDecision symbol does not match Spot position"
            )
        if authorization.side.upper() != "LONG":
            raise SpotAuthorizationRejected("Spot exits only support LONG inventory")
        if authorization.action not in {ExitAction.EXIT, ExitAction.MANAGE}:
            raise SpotAuthorizationRejected(
                "ExitDecision does not authorize a Spot sell"
            )
        if quantity <= 0:
            raise SpotAuthorizationRejected("exit quantity must be positive")
        if self._has_unresolved_order(symbol):
            raise SpotAuthorizationRejected(
                "unresolved Spot order exists for this symbol"
            )

        position = self.inventory.get_position(symbol)
        if position is None or position.quantity < quantity:
            raise SpotAuthorizationRejected("sell exceeds AITOS-managed Spot inventory")
        if authorization.action is ExitAction.MANAGE:
            fraction = Decimal(str(authorization.suggested_reduce_fraction))
            if not Decimal("0") < fraction <= Decimal("1"):
                raise SpotAuthorizationRejected(
                    "MANAGE decision has no valid reduce fraction"
                )
            if quantity > position.quantity * fraction:
                raise SpotAuthorizationRejected(
                    "Spot sell exceeds EIE suggested reduce fraction"
                )

        client_order_id = client_order_id or ("aitos-" + uuid.uuid4().hex[:28])
        request = SpotOrderRequest(
            symbol=symbol,
            side=SpotOrderSide.SELL,
            order_type=SpotOrderType.MARKET,
            quantity=quantity,
            client_order_id=client_order_id,
        )
        self.inventory.record_order_intent(
            client_order_id=client_order_id,
            symbol=symbol,
            side=request.side.value,
            order_type=request.order_type.value,
            quantity=quantity,
        )
        try:
            order = await self.executor.submit(request)
        except Exception:
            self.inventory.update_order_intent(client_order_id, status="UNKNOWN")
            raise

        self.inventory.update_order_intent(
            client_order_id,
            status=order.status.value,
            exchange_order_id=order.order_id,
        )
        if self._definitively_unfilled(order):
            self.release_after_confirmed_flat(symbol)
        return order

    async def reconcile(self, symbol: str, client_order_id: str) -> SpotOrder:
        order = await self.executor.reconcile_order(
            symbol, client_order_id, self.inventory
        )
        self.inventory.update_order_intent(
            client_order_id,
            status=order.status.value,
            exchange_order_id=order.order_id,
        )
        if self._terminal(order):
            self.release_after_confirmed_flat(symbol)
        return order

    def release_after_confirmed_flat(self, symbol: str) -> bool:
        key = InstrumentKey(Venue.BINANCE, MarketType.SPOT, symbol)
        position = self.inventory.get_position(symbol)
        if position is not None and position.quantity > 0:
            return False
        if self._has_unresolved_order(symbol):
            return False
        return self.capital_gateway.release(key.value) is not None

    def _has_unresolved_order(self, symbol: str) -> bool:
        return any(
            item["symbol"] == symbol.upper()
            for item in self.inventory.unresolved_order_intents()
        )

    @staticmethod
    def _terminal(order: SpotOrder) -> bool:
        return order.status in {
            SpotOrderStatus.FILLED,
            SpotOrderStatus.REJECTED,
            SpotOrderStatus.CANCELED,
            SpotOrderStatus.EXPIRED,
        }

    @staticmethod
    def _definitively_unfilled(order: SpotOrder) -> bool:
        return (
            order.status
            in {
                SpotOrderStatus.REJECTED,
                SpotOrderStatus.CANCELED,
                SpotOrderStatus.EXPIRED,
            }
            and order.executed_quantity == 0
        )
