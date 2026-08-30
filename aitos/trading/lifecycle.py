"""Trade Lifecycle — spec section 30.

    [OPPORTUNITY_DETECTED]
        v (Smart Entry Validation passes)
    [ENTRY_VALIDATED]
        v (Dynamic Position Sizing + Adaptive Leverage calculated)
    [ORDER_SUBMITTED]
        v (Order filled)
    [POSITION_OPENED]
        v  (Smart SL/TP, trailing SL, break-even, partial TP monitored via update_price)
    [EXIT_TRIGGERED]
        v (Order filled)
    [POSITION_CLOSED]

Journal/Review/Learning-feedback stages are a later phase (Journal System).
This module owns validation (Risk Engine veto + hard limits + governance),
sizing, order submission, and exit monitoring — wiring the Risk Engine and
AI Kernel into an actual trade, end to end.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventPriority,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError, TradeNotFoundError
from aitos.eventbus.redis_bus import EventBus
from aitos.execution.order_executor import (
    OrderExecutor,
    OrderRequest,
    SimulatedOrderExecutor,
)
from aitos.kernel.ai_kernel import Action, AIKernel
from aitos.logging_setup import get_logger
from aitos.models.trade import (
    Opportunity,
    PartialExit,
    Trade,
    TradeLifecycleState,
    TradeSide,
    new_trade_id,
    utc_now_iso,
)
from aitos.risk.models import PortfolioState, PositionExposure, PositionSizeResult
from aitos.risk.risk_engine import RiskEngine

logger = get_logger("aitos.trading.lifecycle")

TOPIC_OPPORTUNITY = "decision.opportunity"
TOPIC_ENTRY_SIGNAL = "decision.entry"
TOPIC_REJECTED = "trade.rejected"
TOPIC_ORDER_SUBMITTED = "trade.order_submitted"
TOPIC_ORDER_FILLED = "trade.order_filled"
TOPIC_POSITION_OPENED = "trade.position_opened"
TOPIC_POSITION_UPDATED = "trade.position_updated"
TOPIC_POSITION_CLOSED = "trade.position_closed"
TOPIC_SL_TRIGGERED = "trade.sl_triggered"
TOPIC_TP_TRIGGERED = "trade.tp_triggered"
TOPIC_TRAILING_SL_UPDATE = "trade.trailing_sl"
TOPIC_PARTIAL_CLOSE = "trade.partial_close"
TOPIC_EXCHANGE_STOPS_PLACED = "trade.exchange_stops_placed"
DEFAULT_PARTIAL_CLOSE_FRACTION = 0.5


def _valid_market_price(value: Any) -> bool:
    """Return True only for finite, strictly positive market prices."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(price) and price > 0.0


class TradeLifecycle(AITOSModule):
    def __init__(
        self,
        event_bus: EventBus,
        risk_engine: RiskEngine,
        order_executor: OrderExecutor | None = None,
        kernel: AIKernel | None = None,
        use_exchange_side_stops: bool = False,
    ) -> None:
        self._event_bus = event_bus
        self._risk_engine = risk_engine
        self._order_executor = order_executor or SimulatedOrderExecutor()
        self._kernel = kernel
        self._use_exchange_side_stops = (
            use_exchange_side_stops
            and self._order_executor.supports_exchange_side_stops
        )
        self._initialized = False
        self._open_trades: dict[str, Trade] = {}
        self._closed_trades: list[Trade] = []
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return "trade-lifecycle"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._initialized = True
        logger.info("TradeLifecycle initialized")

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "open_trades": len(self._open_trades),
                "closed_trades": len(self._closed_trades),
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        logger.info(
            "TradeLifecycle shut down",
            extra={"aitos_extra": {"open_trades": len(self._open_trades)}},
        )

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield

    async def handle_event(self, event: Event) -> EventResponse | None:
        if not self._initialized:
            return None
        if event.topic.startswith("market.kline.") or event.topic.startswith(
            "market.trade."
        ):
            symbol = event.payload.get("symbol")
            price = event.payload.get("close", event.payload.get("price"))
            if symbol and _valid_market_price(price):
                current_price = float(price)
                for trade in list(self._open_trades.values()):
                    if trade.symbol == symbol:
                        await self.update_price(trade.trade_id, current_price)
            elif symbol and price is not None:
                logger.warning(
                    "Ignoring invalid market price",
                    extra={"aitos_extra": {"symbol": symbol, "price": price}},
                )
        return None

    def get_open_trades(self) -> list[Trade]:
        return list(self._open_trades.values())

    def get_closed_trades(self) -> list[Trade]:
        return list(self._closed_trades)

    async def submit_opportunity(
        self, opportunity: Opportunity, portfolio: PortfolioState
    ) -> Trade:
        self._require_initialized()
        await self._event_bus.publish(
            Event(
                topic=TOPIC_OPPORTUNITY,
                payload={
                    "symbol": opportunity.symbol,
                    "side": opportunity.side.value,
                    "confidence": opportunity.confidence,
                },
                source_module=self.module_id,
            )
        )
        should_veto, veto_reason = self._risk_engine.veto(portfolio)
        if should_veto:
            return await self._reject(opportunity, f"risk veto: {veto_reason}")
        hard_breaches = [
            b for b in self._risk_engine.check_limits(portfolio) if b.is_hard_cap
        ]
        if hard_breaches:
            return await self._reject(
                opportunity, f"hard limit breach: {hard_breaches[0].message}"
            )
        if opportunity.is_production and self._kernel is not None:
            action = Action(
                action_type="order.submit",
                payload={"symbol": opportunity.symbol, "side": opportunity.side.value},
                is_production=True,
                approved_by=opportunity.approved_by,
            )
            governance = await self._kernel.enforce_governance(action)
            if not governance.approved:
                return await self._reject(
                    opportunity, f"governance denied: {governance.reason}"
                )
        return await self._validate_and_open(opportunity, portfolio)

    async def update_price(self, trade_id: str, current_price: float) -> Trade:
        self._require_initialized()
        if not _valid_market_price(current_price):
            raise ValueError(
                f"current_price must be a finite positive number, got {current_price!r}"
            )
        trade = self._open_trades.get(trade_id)
        if trade is None:
            raise TradeNotFoundError(f"No open trade with id '{trade_id}'")
        if trade.state != TradeLifecycleState.POSITION_OPENED:
            return trade
        is_long = trade.side == TradeSide.LONG
        sl_hit = (
            current_price <= trade.sl_price
            if is_long
            else current_price >= trade.sl_price
        )
        if sl_hit:
            return await self._trigger_exit(
                trade, current_price, "sl_triggered", TOPIC_SL_TRIGGERED
            )
        if trade.take_profit_levels:
            next_tp = trade.take_profit_levels[0]
            tp_hit = current_price >= next_tp if is_long else current_price <= next_tp
            if tp_hit:
                if len(trade.take_profit_levels) > 1:
                    await self._partial_close(
                        trade, current_price, DEFAULT_PARTIAL_CLOSE_FRACTION
                    )
                    trade.take_profit_levels.pop(0)
                    if self._use_exchange_side_stops and trade.tp_order_ids:
                        consumed_order_id = trade.tp_order_ids.pop(0)
                        await self._order_executor.cancel_resting_order(
                            trade.symbol, consumed_order_id
                        )
                    return trade
                return await self._trigger_exit(
                    trade, current_price, "tp_triggered", TOPIC_TP_TRIGGERED
                )
        if (
            trade.breakeven_at_r_multiple is not None
            and not trade.breakeven_triggered
            and trade.unrealized_r_multiple(current_price)
            >= trade.breakeven_at_r_multiple
        ):
            trade.sl_price = trade.entry_price
            trade.breakeven_triggered = True
            trade.updated_at = utc_now_iso()
            if self._use_exchange_side_stops:
                await self._replace_exchange_side_stop_loss(trade)
            await self._event_bus.publish(
                Event(
                    topic=TOPIC_POSITION_UPDATED,
                    payload={**trade.to_dict(), "reason": "breakeven"},
                    source_module=self.module_id,
                )
            )
        if trade.trailing_sl_enabled:
            candidate_sl = (
                current_price - trade.r_distance
                if is_long
                else current_price + trade.r_distance
            )
            improved = (is_long and candidate_sl > trade.sl_price) or (
                not is_long and candidate_sl < trade.sl_price
            )
            if improved:
                trade.sl_price = candidate_sl
                trade.updated_at = utc_now_iso()
                if self._use_exchange_side_stops:
                    await self._replace_exchange_side_stop_loss(trade)
                await self._event_bus.publish(
                    Event(
                        topic=TOPIC_TRAILING_SL_UPDATE,
                        payload={**trade.to_dict()},
                        source_module=self.module_id,
                    )
                )
        return trade

    async def close_trade(self, trade_id: str, exit_price: float, reason: str) -> Trade:
        self._require_initialized()
        if not _valid_market_price(exit_price):
            raise ValueError(
                f"exit_price must be a finite positive number, got {exit_price!r}"
            )
        trade = self._open_trades.pop(trade_id, None)
        if trade is None:
            raise TradeNotFoundError(f"No open trade with id '{trade_id}'")
        direction = 1 if trade.side == TradeSide.LONG else -1
        pnl_pct = ((exit_price - trade.entry_price) / trade.entry_price) * direction
        realized_on_remaining = trade.position_size_usd * pnl_pct
        trade.pnl = (
            round((trade.pnl or 0.0) + realized_on_remaining, 4)
            if trade.partial_exits
            else round(realized_on_remaining, 4)
        )
        trade.pnl_percent = round(pnl_pct * 100, 4)
        trade.exit_price = exit_price
        trade.exit_time = utc_now_iso()
        trade.exit_reason = reason
        trade.state = TradeLifecycleState.POSITION_CLOSED
        trade.updated_at = trade.exit_time
        self._closed_trades.append(trade)
        if self._use_exchange_side_stops:
            await self._cancel_resting_orders(trade)
        await self._event_bus.publish(
            Event(
                topic=TOPIC_POSITION_CLOSED,
                payload=trade.to_dict(),
                source_module=self.module_id,
            )
        )
        self._last_event_time = trade.updated_at
        return trade

    async def reconcile_trade(self, trade_id: str) -> Trade:
        self._require_initialized()
        trade = self._open_trades.get(trade_id)
        if trade is None:
            raise TradeNotFoundError(f"No open trade with id '{trade_id}'")
        if (
            not self._use_exchange_side_stops
            or trade.state != TradeLifecycleState.POSITION_OPENED
        ):
            return trade
        if trade.sl_order_id:
            status = await self._order_executor.get_resting_order_status(
                trade.symbol, trade.sl_order_id
            )
            if status == "FILLED":
                return await self.close_trade(
                    trade.trade_id, trade.sl_price, "sl_triggered_exchange"
                )
        if trade.tp_order_ids:
            status = await self._order_executor.get_resting_order_status(
                trade.symbol, trade.tp_order_ids[0]
            )
            if status == "FILLED":
                trigger_price = (
                    trade.take_profit_levels[0]
                    if trade.take_profit_levels
                    else trade.tp_price
                )
                if len(trade.take_profit_levels) > 1:
                    await self._partial_close(
                        trade, trigger_price, DEFAULT_PARTIAL_CLOSE_FRACTION
                    )
                    trade.take_profit_levels.pop(0)
                    trade.tp_order_ids.pop(0)
                    return trade
                return await self.close_trade(
                    trade.trade_id, trigger_price, "tp_triggered_exchange"
                )
        return trade

    async def _place_exchange_side_stops(self, trade: Trade) -> None:
        sl_result = await self._order_executor.place_stop_loss_order(
            trade.symbol, trade.side, trade.quantity, trade.sl_price
        )
        if sl_result.success:
            trade.sl_order_id = sl_result.order_id
        else:
            logger.error(
                "failed to place exchange-side stop loss",
                extra={
                    "aitos_extra": {
                        "trade_id": trade.trade_id,
                        "error": sl_result.error,
                    }
                },
            )
        remaining_qty = trade.quantity
        levels = trade.take_profit_levels
        for i, level_price in enumerate(levels):
            leg_qty = (
                remaining_qty
                if i == len(levels) - 1
                else remaining_qty * DEFAULT_PARTIAL_CLOSE_FRACTION
            )
            remaining_qty -= leg_qty
            tp_result = await self._order_executor.place_take_profit_order(
                trade.symbol, trade.side, leg_qty, level_price
            )
            if tp_result.success:
                trade.tp_order_ids.append(tp_result.order_id)
            else:
                logger.error(
                    "failed to place exchange-side take profit",
                    extra={
                        "aitos_extra": {
                            "trade_id": trade.trade_id,
                            "error": tp_result.error,
                        }
                    },
                )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_EXCHANGE_STOPS_PLACED,
                payload={**trade.to_dict()},
                source_module=self.module_id,
            )
        )

    async def _replace_exchange_side_stop_loss(self, trade: Trade) -> None:
        if trade.sl_order_id:
            await self._order_executor.cancel_resting_order(
                trade.symbol, trade.sl_order_id
            )
        result = await self._order_executor.place_stop_loss_order(
            trade.symbol, trade.side, trade.quantity, trade.sl_price
        )
        trade.sl_order_id = result.order_id if result.success else None

    async def _cancel_resting_orders(self, trade: Trade) -> None:
        if trade.sl_order_id:
            await self._order_executor.cancel_resting_order(
                trade.symbol, trade.sl_order_id
            )
            trade.sl_order_id = None
        for order_id in trade.tp_order_ids:
            await self._order_executor.cancel_resting_order(trade.symbol, order_id)
        trade.tp_order_ids.clear()

    def _project_portfolio(
        self,
        portfolio: PortfolioState,
        opportunity: Opportunity,
        sizing: PositionSizeResult,
    ) -> PortfolioState:
        candidate = PositionExposure(
            symbol=opportunity.symbol,
            notional_usd=sizing.position_size_usd,
            leverage=sizing.leverage,
        )
        return PortfolioState(
            equity_usd=portfolio.equity_usd,
            peak_equity_usd=portfolio.peak_equity_usd,
            positions=(*portfolio.positions, candidate),
            daily_pnl_pct=portfolio.daily_pnl_pct,
            weekly_pnl_pct=portfolio.weekly_pnl_pct,
            volatility_percentile=portfolio.volatility_percentile,
            regime=portfolio.regime,
            max_pairwise_correlation=portfolio.max_pairwise_correlation,
            api_error_rate_pct=portfolio.api_error_rate_pct,
            api_latency_ms=portfolio.api_latency_ms,
            data_freshness_seconds=portfolio.data_freshness_seconds,
            model_accuracy=portfolio.model_accuracy,
            timestamp=portfolio.timestamp,
        )

    async def _validate_and_open(
        self, opportunity: Opportunity, portfolio: PortfolioState
    ) -> Trade:
        from aitos.risk.position_sizing import calculate_position_size
        from aitos.risk.sector import sector_for_symbol

        risk_score = (
            self._risk_engine.last_assessment.total
            if self._risk_engine.last_assessment
            else 0.0
        )
        candidate_sector = sector_for_symbol(opportunity.symbol)
        existing_sector_notional = sum(
            p.notional_usd for p in portfolio.positions if p.sector == candidate_sector
        )
        sizing = calculate_position_size(
            equity_usd=portfolio.equity_usd,
            entry_price=opportunity.entry_price,
            stop_loss_price=opportunity.stop_loss_price,
            risk_limits=self._risk_engine.limits,
            risk_score=risk_score,
            volatility_percentile=portfolio.volatility_percentile,
            correlation_penalty=portfolio.max_pairwise_correlation,
            existing_sector_notional_usd=existing_sector_notional,
            sector_limit_pct=self._risk_engine.limits.max_sector_exposure_pct,
        )
        if sizing.position_size_usd <= 0.0:
            return await self._reject(
                opportunity, f"sector exposure cap exhausted for {candidate_sector}"
            )

        projected_portfolio = self._project_portfolio(portfolio, opportunity, sizing)
        projected_hard_breaches = [
            b
            for b in self._risk_engine.check_limits(projected_portfolio)
            if b.is_hard_cap
        ]
        if projected_hard_breaches:
            return await self._reject(
                opportunity,
                f"projected hard limit breach: {projected_hard_breaches[0].message}",
            )

        await self._event_bus.publish(
            Event(
                topic=TOPIC_ENTRY_SIGNAL,
                payload={
                    "symbol": opportunity.symbol,
                    "side": opportunity.side.value,
                    "sizing_rationale": sizing.rationale,
                },
                source_module=self.module_id,
            )
        )
        quantity = sizing.position_size_usd / opportunity.entry_price
        order_result = await self._order_executor.submit_order(
            OrderRequest(
                symbol=opportunity.symbol,
                side=opportunity.side,
                quantity=quantity,
                reference_price=opportunity.entry_price,
            )
        )
        if not order_result.success:
            return await self._reject(
                opportunity, f"order submission failed: {order_result.error}"
            )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_ORDER_SUBMITTED,
                payload={
                    "symbol": opportunity.symbol,
                    "side": opportunity.side.value,
                    "quantity": quantity,
                },
                source_module=self.module_id,
            )
        )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_ORDER_FILLED,
                payload={
                    "order_id": order_result.order_id,
                    "fill_price": order_result.fill_price,
                },
                source_module=self.module_id,
            )
        )
        trade = Trade(
            trade_id=new_trade_id(),
            symbol=opportunity.symbol,
            side=opportunity.side,
            entry_price=order_result.fill_price,
            quantity=order_result.filled_quantity,
            leverage=sizing.leverage,
            position_size_usd=sizing.position_size_usd,
            risk_amount_usd=sizing.risk_amount_usd,
            strategy_id=opportunity.strategy_id,
            agent_consensus=opportunity.agent_consensus,
            explanation=opportunity.rationale,
            sl_price=opportunity.stop_loss_price,
            tp_price=(
                opportunity.take_profit_levels[0]
                if opportunity.take_profit_levels
                else opportunity.entry_price
            ),
            take_profit_levels=list(opportunity.take_profit_levels),
            state=TradeLifecycleState.POSITION_OPENED,
            entry_time=utc_now_iso(),
            trailing_sl_enabled=opportunity.trailing_sl_enabled,
            breakeven_at_r_multiple=opportunity.breakeven_at_r_multiple,
            regime=opportunity.regime,
        )
        self._open_trades[trade.trade_id] = trade
        await self._event_bus.publish(
            Event(
                topic=TOPIC_POSITION_OPENED,
                payload=trade.to_dict(),
                source_module=self.module_id,
            )
        )
        self._last_event_time = trade.entry_time
        if self._use_exchange_side_stops:
            await self._place_exchange_side_stops(trade)
        return trade

    async def _reject(self, opportunity: Opportunity, reason: str) -> Trade:
        trade = Trade(
            trade_id=new_trade_id(),
            symbol=opportunity.symbol,
            side=opportunity.side,
            entry_price=opportunity.entry_price,
            quantity=0.0,
            leverage=0.0,
            position_size_usd=0.0,
            risk_amount_usd=0.0,
            strategy_id=opportunity.strategy_id,
            agent_consensus=opportunity.agent_consensus,
            explanation=opportunity.rationale,
            sl_price=opportunity.stop_loss_price,
            tp_price=(
                opportunity.take_profit_levels[0]
                if opportunity.take_profit_levels
                else opportunity.entry_price
            ),
            take_profit_levels=list(opportunity.take_profit_levels),
            state=TradeLifecycleState.REJECTED,
            entry_time=utc_now_iso(),
            rejection_reason=reason,
        )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_REJECTED,
                payload=trade.to_dict(),
                source_module=self.module_id,
            )
        )
        logger.info(
            "opportunity rejected",
            extra={"aitos_extra": {"symbol": opportunity.symbol, "reason": reason}},
        )
        return trade

    async def _trigger_exit(
        self, trade: Trade, price: float, reason: str, topic: str
    ) -> Trade:
        trade.state = TradeLifecycleState.EXIT_TRIGGERED
        trade.updated_at = utc_now_iso()
        await self._event_bus.publish(
            Event(
                topic=topic,
                payload={**trade.to_dict(), "trigger_price": price},
                source_module=self.module_id,
                priority=EventPriority.HIGH,
            )
        )
        return await self.close_trade(trade.trade_id, price, reason)

    async def _partial_close(
        self, trade: Trade, price: float, close_fraction: float
    ) -> None:
        direction = 1 if trade.side == TradeSide.LONG else -1
        pnl_pct = ((price - trade.entry_price) / trade.entry_price) * direction
        closed_notional = trade.position_size_usd * close_fraction
        realized = closed_notional * pnl_pct
        trade.partial_exits.append(
            PartialExit(
                price=price,
                size_usd=round(closed_notional, 2),
                r_multiple=round(trade.unrealized_r_multiple(price), 4),
            )
        )
        trade.position_size_usd = round(trade.position_size_usd - closed_notional, 2)
        trade.pnl = round((trade.pnl or 0.0) + realized, 4)
        trade.updated_at = utc_now_iso()
        await self._event_bus.publish(
            Event(
                topic=TOPIC_PARTIAL_CLOSE,
                payload={**trade.to_dict(), "closed_notional_usd": closed_notional},
                source_module=self.module_id,
            )
        )

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "TradeLifecycle.initialize() must be called first"
            )
