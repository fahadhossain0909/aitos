"""Capital-gated Spot entry and inventory-bounded exit orchestration."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.market_data.venues import InstrumentKey, MarketType, Venue
from aitos.models.trade import Opportunity, TradeSide
from aitos.spot.execution import SpotOrderExecutor
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
        scoped_opportunity = replace(opportunity, symbol=key.value)
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
        self.inventory.record_order_intent(
            client_order_id=client_order_id,
            symbol=request.symbol,
            side=request.side.value,
            order_type=request.order_type.value,
            quantity=request.quantity,
            quote_order_quantity=request.quote_order_quantity,
        )

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
        if self._terminal(order):
            self.release_after_confirmed_flat(request.symbol)
        return order

    async def submit_exit(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        exit_authorized: bool,
        client_order_id: str | None = None,
    ) -> SpotOrder:
        """Submit an inventory-bounded Spot sell after lifecycle authorization.

        Spot has no exchange reduceOnly flag. The caller must pass an explicit
        authorization from the Exit Intelligence/lifecycle boundary.
        """
        if not exit_authorized:
            raise SpotAuthorizationRejected(
                "Spot exits require explicit Exit Intelligence authorization"
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
        if self._terminal(order):
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
        if self._terminal_without_fills(order):
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
            SpotOrderStatus.REJECTED,
            SpotOrderStatus.CANCELED,
            SpotOrderStatus.EXPIRED,
        }
