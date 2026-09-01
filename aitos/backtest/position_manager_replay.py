"""Canonical PositionManager replay for paired conditional-hedge benchmarks.

The replay deliberately keeps entry generation simple and deterministic (shared
auction score baseline) while routing every open-position management decision
through the real PositionManager.  Hedge ON/OFF runs consume the identical
ordered market events and execution model.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from aitos.backtest.hedge_metrics import TradeExcursion, excursions, max_drawdown
from aitos.backtest.l2_execution import BookLevel, L2ExecutionModel
from aitos.backtest.market_adapter import HistoricalMarketAdapter, HistoricalMarketState
from aitos.intelligence.amt.volume_profile import build_volume_profile
from aitos.intelligence.hedge_intelligence import HedgeAction
from aitos.intelligence.order_flow_engine import OrderFlowFeatures
from aitos.models.market import OrderBookSnapshot, TradeTick
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.position_manager import PositionManager


@dataclass(frozen=True)
class PositionReplayResult:
    hedge_enabled: bool
    states: int
    primary_entries: int
    primary_exits: int
    hedge_opens: int
    hedge_closes: int
    initial_equity: float
    final_equity: float
    net_pnl: float
    total_fees: float
    hedge_fees: float
    hedge_execution_cost: float
    hedge_pnl: float
    equity_curve: tuple[float, ...]
    trade_pnls: tuple[float, ...]
    trade_excursions: tuple[TradeExcursion, ...]
    hedge_durations_seconds: tuple[float, ...]

    @property
    def total_return(self) -> float:
        return self.net_pnl / self.initial_equity if self.initial_equity else 0.0

    @property
    def max_drawdown(self) -> float:
        return max_drawdown(self.equity_curve)

    @property
    def expectancy(self) -> float:
        return sum(self.trade_pnls) / len(self.trade_pnls) if self.trade_pnls else 0.0

    @property
    def mae(self) -> float:
        return min((x.mae for x in self.trade_excursions), default=0.0)

    @property
    def mfe(self) -> float:
        return max((x.mfe for x in self.trade_excursions), default=0.0)


@dataclass
class _Leg:
    side: TradeSide
    quantity: float = 0.0
    entry_price: float = 0.0
    realized_pnl: float = 0.0
    opened_at: datetime | None = None

    def add(self, quantity: float, price: float) -> None:
        if quantity <= 0:
            return
        if self.quantity <= 0:
            self.quantity = quantity
            self.entry_price = price
            return
        total = self.quantity + quantity
        self.entry_price = (
            (self.entry_price * self.quantity) + (price * quantity)
        ) / total
        self.quantity = total

    def reduce(self, quantity: float, price: float) -> float:
        quantity = min(max(quantity, 0.0), self.quantity)
        if quantity <= 0:
            return 0.0
        sign = 1.0 if self.side == TradeSide.LONG else -1.0
        pnl = (price - self.entry_price) * sign * quantity
        self.realized_pnl += pnl
        self.quantity -= quantity
        if self.quantity <= 1e-12:
            self.quantity = 0.0
            self.entry_price = 0.0
            self.opened_at = None
        return pnl

    def unrealized(self, price: float) -> float:
        if self.quantity <= 0 or price <= 0:
            return 0.0
        sign = 1.0 if self.side == TradeSide.LONG else -1.0
        return (price - self.entry_price) * sign * self.quantity


class PositionManagerHistoricalReplay:
    """Replay canonical PositionManager decisions with optional hedge overlay."""

    def __init__(
        self,
        *,
        symbol: str,
        tick_size: float,
        initial_cash: float = 10_000.0,
        fee_rate: float = 0.0004,
        max_book_levels: int | None = None,
        trade_window: int = 500,
        hedge_enabled: bool = False,
        hedge_config: dict[str, object] | None = None,
    ) -> None:
        self.symbol = symbol
        self.tick_size = tick_size
        self.initial_cash = initial_cash
        self.fee_rate = fee_rate
        self.adapter = HistoricalMarketAdapter(symbol, tick_size, trade_window)
        self.l2 = L2ExecutionModel(max_levels=max_book_levels)
        config = dict(hedge_config or {})
        config["enabled"] = hedge_enabled
        self.position_manager = PositionManager(config={"hedge": config})
        self.primary: _Leg | None = None
        self.hedge: _Leg | None = None
        self.trade: Trade | None = None
        self.equity = initial_cash
        self.fees = 0.0
        self.hedge_fees = 0.0
        self.hedge_execution_cost = 0.0
        self.hedge_realized = 0.0
        self.states = 0
        self.primary_entries = 0
        self.primary_exits = 0
        self.hedge_opens = 0
        self.hedge_closes = 0
        self.equity_curve: list[float] = []
        self.trade_pnls: list[float] = []
        self.trade_excursions: list[TradeExcursion] = []
        self._primary_prices: list[float] = []
        self._hedge_opened_at: datetime | None = None
        self.hedge_durations_seconds: list[float] = []

    def _order(
        self, side: str, quantity: float, state: HistoricalMarketState
    ) -> tuple[float, float]:
        if quantity <= 0 or state.latest_order_book is None:
            return 0.0, 0.0
        book = state.latest_order_book
        bids = [BookLevel(x.price, x.quantity) for x in book.bids]
        asks = [BookLevel(x.price, x.quantity) for x in book.asks]
        result = self.l2.execute(side, quantity, bids, asks)
        if result.filled_quantity <= 0:
            return 0.0, 0.0
        mid = (
            (book.best_bid + book.best_ask) / 2.0
            if book.best_bid > 0 and book.best_ask > 0
            else result.average_price
        )
        fee = result.filled_quantity * result.average_price * self.fee_rate
        execution_cost = abs(result.average_price - mid) * result.filled_quantity
        self.fees += fee
        return result.filled_quantity, result.average_price

    @staticmethod
    def _entry_decision(state: HistoricalMarketState) -> tuple[TradeSide, float] | None:
        if state.auction_long_score < 0.55 and state.auction_short_score < 0.55:
            return None
        if state.auction_long_score > state.auction_short_score:
            return TradeSide.LONG, state.auction_long_score
        return TradeSide.SHORT, state.auction_short_score

    def _open_primary(
        self, state: HistoricalMarketState, side: TradeSide, confidence: float
    ) -> None:
        if state.latest_order_book is None:
            return
        qty, price = self._order(
            "buy" if side == TradeSide.LONG else "sell", 1.0, state
        )
        if qty <= 0:
            return
        self.primary = _Leg(
            side,
            qty,
            price,
            opened_at=state.latest_trade.timestamp if state.latest_trade else None,
        )
        self.trade = Trade(
            trade_id=f"bt-{self.states}",
            symbol=self.symbol,
            side=side,
            entry_price=price,
            quantity=qty,
            leverage=1.0,
            position_size_usd=price * qty,
            risk_amount_usd=abs(price - (price * 0.995)) * qty,
            strategy_id="historical_auction_pm",
            agent_consensus={"auction_score": confidence},
            explanation="Deterministic historical benchmark entry",
            sl_price=price * (0.995 if side == TradeSide.LONG else 1.005),
            tp_price=price * (1.01 if side == TradeSide.LONG else 0.99),
            state=TradeLifecycleState.POSITION_OPENED,
            entry_time=(
                state.latest_trade.timestamp.isoformat() if state.latest_trade else ""
            ),
        )
        self.position_manager.clear_trade(self.trade.trade_id, self.symbol)
        self.primary_entries += 1
        self._primary_prices = [price]

    def _close_primary(
        self, state: HistoricalMarketState, fraction: float = 1.0
    ) -> None:
        if self.primary is None or self.primary.quantity <= 0:
            return
        qty = self.primary.quantity * max(0.0, min(1.0, fraction))
        side = "sell" if self.primary.side == TradeSide.LONG else "buy"
        filled, price = self._order(side, qty, state)
        if filled <= 0:
            return
        pnl = self.primary.reduce(filled, price)
        if self.trade is not None:
            self.trade.record_excursion(price)
        if self.primary.quantity <= 0:
            self.trade_pnls.append(pnl)
            if self.trade is not None:
                self.trade_excursions.append(
                    excursions(
                        self.trade.entry_price,
                        self.trade.side.value,
                        self._primary_prices + [price],
                    )
                )
                self.position_manager.clear_trade(self.trade.trade_id, self.symbol)
            self.trade = None
            self.primary = None
            self.primary_exits += 1
            self._primary_prices = []

    def _open_or_resize_hedge(self, state: HistoricalMarketState, ratio: float) -> None:
        if self.primary is None or self.primary.quantity <= 0:
            return
        target = self.primary.quantity * max(0.0, min(1.0, ratio))
        hedge_side = (
            TradeSide.SHORT if self.primary.side == TradeSide.LONG else TradeSide.LONG
        )
        if self.hedge is None:
            self.hedge = _Leg(hedge_side)
        delta = target - self.hedge.quantity
        if delta > 1e-12:
            side = "buy" if hedge_side == TradeSide.LONG else "sell"
            filled, price = self._order(side, delta, state)
            if filled > 0:
                book = state.latest_order_book
                mid = (
                    ((book.best_bid + book.best_ask) / 2.0)
                    if book and book.best_bid > 0 and book.best_ask > 0
                    else price
                )
                self.hedge_execution_cost += abs(price - mid) * filled
                self.hedge.add(filled, price)
                self.hedge_fees += filled * price * self.fee_rate
                if self.hedge.opened_at is None:
                    self.hedge.opened_at = (
                        state.latest_trade.timestamp if state.latest_trade else None
                    )
                    self._hedge_opened_at = self.hedge.opened_at
                    self.hedge_opens += 1
        elif delta < -1e-12:
            self._close_hedge(state, -delta)

    def _close_hedge(
        self, state: HistoricalMarketState, quantity: float | None = None
    ) -> None:
        if self.hedge is None or self.hedge.quantity <= 0:
            return
        qty = (
            self.hedge.quantity
            if quantity is None
            else min(quantity, self.hedge.quantity)
        )
        side = "sell" if self.hedge.side == TradeSide.LONG else "buy"
        filled, price = self._order(side, qty, state)
        if filled <= 0:
            return
        pnl = self.hedge.reduce(filled, price)
        self.hedge_realized += pnl
        if self.hedge.quantity <= 0:
            opened = self._hedge_opened_at
            closed = state.latest_trade.timestamp if state.latest_trade else None
            if opened and closed:
                self.hedge_durations_seconds.append(
                    max(0.0, (closed - opened).total_seconds())
                )
            self._hedge_opened_at = None
            self.hedge_closes += 1
            self.hedge = None

    def on_event(self, event: TradeTick | OrderBookSnapshot) -> None:
        if isinstance(event, TradeTick):
            self.adapter.on_trade(event)
            price = event.price
        else:
            self.adapter.on_order_book(event)
            book = event
            price = (
                (book.best_bid + book.best_ask) / 2.0
                if book.best_bid > 0 and book.best_ask > 0
                else 0.0
            )
        if price <= 0:
            return
        state = self.adapter.state()
        self.states += 1

        if self.primary is None:
            candidate = self._entry_decision(state)
            if candidate:
                self._open_primary(state, *candidate)
        else:
            if self.trade is None:
                return
            self.trade.record_excursion(price)
            self._primary_prices.append(price)
            if len(self._primary_prices) > 500:
                self._primary_prices.pop(0)
            of: OrderFlowFeatures = self.adapter.order_flow.snapshot()
            profile = build_volume_profile(
                self.adapter.order_flow.trades, self.tick_size
            )
            action = self.position_manager.evaluate(
                trade=self.trade,
                current_price=price,
                order_flow=of,
                volume_profile=profile,
                liquidity_events=state.liquidity_events,
                prior_highs=(profile.high,) if profile.high else (),
                prior_lows=(profile.low,) if profile.low else (),
                swing_highs=(profile.high,) if profile.high else (),
                swing_lows=(profile.low,) if profile.low else (),
                timestamp=event.timestamp,
            )
            if action.action.value == "EXIT":
                self._close_primary(state, 1.0)
            elif action.action.value == "MANAGE" and action.reduce_fraction > 0:
                self._close_primary(state, action.reduce_fraction)

            hedge = action.hedge_decision
            if hedge is not None:
                if hedge.action == HedgeAction.OPEN or hedge.action == HedgeAction.HOLD:
                    self._open_or_resize_hedge(state, hedge.hedge_ratio)
                elif hedge.action == HedgeAction.CLOSE:
                    self._close_hedge(state)

        hedge_unrealized = self.hedge.unrealized(price) if self.hedge else 0.0
        primary_unrealized = self.primary.unrealized(price) if self.primary else 0.0
        realized = (self.primary.realized_pnl if self.primary else 0.0) + (
            self.hedge_realized + (self.hedge.realized_pnl if self.hedge else 0.0)
        )
        self.equity = (
            self.initial_cash
            + realized
            + primary_unrealized
            + hedge_unrealized
            - self.fees
        )
        self.equity_curve.append(self.equity)

    def finish(self, last_price: float) -> PositionReplayResult:
        if self.primary is not None and self.primary.quantity > 0:
            pnl = self.primary.unrealized(last_price) + self.primary.realized_pnl
            self.trade_pnls.append(pnl)
            if self.trade is not None:
                self.trade_excursions.append(
                    excursions(
                        self.trade.entry_price,
                        self.trade.side.value,
                        self._primary_prices + [last_price],
                    )
                )
        if self.hedge is not None:
            self.hedge_realized += (
                self.hedge.unrealized(last_price) + self.hedge.realized_pnl
            )
        final_equity = (
            self.initial_cash + self.hedge_realized + sum(self.trade_pnls) - self.fees
        )
        return PositionReplayResult(
            hedge_enabled=self.position_manager._hie.enabled,
            states=self.states,
            primary_entries=self.primary_entries,
            primary_exits=self.primary_exits,
            hedge_opens=self.hedge_opens,
            hedge_closes=self.hedge_closes,
            initial_equity=self.initial_cash,
            final_equity=final_equity,
            net_pnl=final_equity - self.initial_cash,
            total_fees=self.fees,
            hedge_fees=self.hedge_fees,
            hedge_execution_cost=self.hedge_execution_cost,
            hedge_pnl=self.hedge_realized,
            equity_curve=tuple(self.equity_curve),
            trade_pnls=tuple(self.trade_pnls),
            trade_excursions=tuple(self.trade_excursions),
            hedge_durations_seconds=tuple(self.hedge_durations_seconds),
        )


def replay(
    events: Iterable[TradeTick | OrderBookSnapshot], **kwargs: object
) -> PositionReplayResult:
    runner = PositionManagerHistoricalReplay(**kwargs)
    last_price = 0.0
    for event in sorted(events, key=lambda item: item.timestamp):
        runner.on_event(event)
        if isinstance(event, TradeTick):
            last_price = event.price
        elif event.best_bid > 0 and event.best_ask > 0:
            last_price = (event.best_bid + event.best_ask) / 2.0
    return runner.finish(last_price)
