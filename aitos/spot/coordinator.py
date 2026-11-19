"""Capital-gated Spot entry orchestration.

Entries are long-only, quote-budgeted market buys. Futures leverage/funding
assumptions are explicitly excluded. Sells are inventory-reducing exits and
must be driven by the shared lifecycle/exit authority, never by this entry API.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import uuid

from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.market_data.venues import InstrumentKey, MarketType, Venue
from aitos.models.trade import Opportunity, TradeSide
from aitos.spot.execution import SpotOrderExecutor
from aitos.spot.inventory import InventoryLedger
from aitos.spot.models import SpotOrder, SpotOrderRequest, SpotOrderSide, SpotOrderType


class SpotAuthorizationRejected(RuntimeError):
    pass


class SpotTradingCoordinator:
    def __init__(self, *, capital_gateway: CapitalGateway,
                 executor: SpotOrderExecutor, inventory: InventoryLedger,
                 fee_bps: float = 10.0, slippage_bps: float = 8.0) -> None:
        if fee_bps < 0 or slippage_bps < 0:
            raise ValueError("Spot costs cannot be negative")
        self.capital_gateway = capital_gateway
        self.executor = executor
        self.inventory = inventory
        self.fee_bps, self.slippage_bps = fee_bps, slippage_bps

    async def submit_entry(self, *, equity_quote: float, opportunity: Opportunity,
                           request: SpotOrderRequest) -> SpotOrder:
        if opportunity.side is not TradeSide.LONG:
            raise SpotAuthorizationRejected("Spot entry is long-only; short exposure is unsupported")
        if request.side is not SpotOrderSide.BUY or request.order_type is not SpotOrderType.MARKET:
            raise SpotAuthorizationRejected("Spot entry requires a market BUY")
        if request.quote_order_quantity is None:
            raise SpotAuthorizationRejected("Spot entry must be bounded by quoteOrderQty")
        key = InstrumentKey(Venue.BINANCE, MarketType.SPOT, request.symbol)
        scoped = replace(opportunity, symbol=key.value)
        decision, allocation = self.capital_gateway.authorize_opportunity(
            equity_quote, scoped, fee_bps=self.fee_bps,
            slippage_bps=self.slippage_bps, funding_bps=0.0,
        )
        if not decision.eligible or allocation is None:
            raise SpotAuthorizationRejected(
                "capital gateway rejected Spot entry: " + ", ".join(decision.rejection_reasons)
            )
        budget = Decimal(str(allocation.capital_usd))
        requested = request.quote_order_quantity
        if requested > budget:
            self.capital_gateway.release(key.value)
            raise SpotAuthorizationRejected("requested quote notional exceeds approved Spot allocation")
        # Pin before network submission. A timeout is ambiguous; retaining the
        # reservation is safer than releasing capital while an order may exist.
        if not self.capital_gateway.pin(key.value):
            self.capital_gateway.release(key.value)
            raise SpotAuthorizationRejected("could not pin Spot capital reservation")
        try:
            return await self.executor.submit(request)
        except Exception:
            # Intentionally retain the pinned reservation: operator recovery
            # must query the client order ID before deciding whether to release.
            raise

    async def submit_exit(self, *, symbol: str, quantity: Decimal,
                          exit_authorized: bool,
                          client_order_id: str | None = None) -> SpotOrder:
        """Submit an inventory-bounded Spot sell after Exit Intelligence approval.

        Spot has no exchange reduceOnly flag, so managed inventory is checked
        locally and the lifecycle must explicitly authorize the exit.
        """
        if not exit_authorized:
            raise SpotAuthorizationRejected(
                "Spot exits require explicit Exit Intelligence authorization"
            )
        if quantity <= 0:
            raise SpotAuthorizationRejected("exit quantity must be positive")
        position = self.inventory.get_position(symbol)
        if position is None or position.quantity < quantity:
            raise SpotAuthorizationRejected(
                "sell exceeds AITOS-managed Spot inventory"
            )
        client_order_id = client_order_id or "aitos-" + uuid.uuid4().hex[:28]
        request = SpotOrderRequest(
            symbol=symbol, side=SpotOrderSide.SELL, order_type=SpotOrderType.MARKET,
            quantity=quantity, client_order_id=client_order_id,
        )
        self.inventory.record_order_intent(
            client_order_id=client_order_id, symbol=symbol, side="SELL",
            order_type="MARKET", quantity=quantity,
        )
        try:
            order = await self.executor.submit(request)
        except Exception:
            self.inventory.update_order_intent(client_order_id, status="UNKNOWN")
            raise
        self.inventory.update_order_intent(
            client_order_id, status=order.status.value, exchange_order_id=order.order_id
        )
        return order

    async def reconcile(self, symbol: str, client_order_id: str) -> SpotOrder:
        order = await self.executor.reconcile_order(symbol, client_order_id, self.inventory)
        self.inventory.update_order_intent(
            client_order_id, status=order.status.value, exchange_order_id=order.order_id
        )
        return order

    def release_after_confirmed_flat(self, symbol: str) -> bool:
        key = InstrumentKey(Venue.BINANCE, MarketType.SPOT, symbol)
        position = self.inventory.get_position(symbol)
        if position is not None and position.quantity > 0:
            return False
        return self.capital_gateway.release(key.value) is not None
