"""Offline exit-policy replay — Phase F.

Given a TradeScenario (entry + initial SL/TP) and a sequence of PriceBars,
simulate two independent exit policies on the *same* path:

1. **static** — classic fixed-R take-profit + stop-loss (optional trailing)
2. **eie**    — Exit Intelligence Engine via PositionManager

No live services, no Redis, no exchange. Fully deterministic given the same
bars and scenario.

Typical use:

```python
from aitos.evaluation import TradeScenario, PriceBar, compare_policies

scenario = TradeScenario(
    symbol="BTCUSDT", side="LONG", entry_price=79000.0,
    stop_loss=78500.0, take_profit_levels=(80000.0,),
)
bars = [PriceBar(ts=..., open=..., high=..., low=..., close=...) for ...]
summary = compare_policies(scenario, bars)
print(summary.to_dict())
```
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from aitos.intelligence.exit_intelligence import ExitAction
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.position_manager import PositionManager

PolicyName = Literal["static", "eie"]


@dataclass(frozen=True)
class PriceBar:
    """Minimal OHLCV bar used for offline replay."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class TradeScenario:
    """Entry definition for a single offline trade replay."""

    symbol: str
    side: str  # LONG | SHORT
    entry_price: float
    stop_loss: float
    take_profit_levels: tuple[float, ...] = ()
    position_size_usd: float = 10_000.0
    quantity: float = 0.0  # if 0, derived from size / entry
    trailing_sl: bool = False
    breakeven_at_r: float | None = None
    strategy_id: str = "offline_eval"

    def resolved_quantity(self) -> float:
        if self.quantity > 0:
            return self.quantity
        if self.entry_price <= 0:
            return 0.0
        return self.position_size_usd / self.entry_price


@dataclass
class ExitPolicyResult:
    """Outcome of one policy on one scenario."""

    policy: PolicyName
    exit_price: float
    exit_reason: str
    exit_bar_index: int
    hold_bars: int
    pnl_usd: float
    pnl_pct: float
    r_multiple: float
    max_favorable_pct: float
    max_adverse_pct: float
    partial_exits: int = 0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "exit_bar_index": self.exit_bar_index,
            "hold_bars": self.hold_bars,
            "pnl_usd": self.pnl_usd,
            "pnl_pct": self.pnl_pct,
            "r_multiple": self.r_multiple,
            "max_favorable_pct": self.max_favorable_pct,
            "max_adverse_pct": self.max_adverse_pct,
            "partial_exits": self.partial_exits,
            "notes": list(self.notes),
        }


@dataclass
class ExitReplaySummary:
    """Side-by-side comparison of static vs eie on one scenario."""

    scenario: TradeScenario
    static: ExitPolicyResult
    eie: ExitPolicyResult
    bars_consumed: int

    @property
    def pnl_delta_usd(self) -> float:
        return self.eie.pnl_usd - self.static.pnl_usd

    @property
    def eie_better(self) -> bool:
        return self.eie.pnl_usd > self.static.pnl_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.scenario.symbol,
            "side": self.scenario.side,
            "entry_price": self.scenario.entry_price,
            "bars_consumed": self.bars_consumed,
            "static": self.static.to_dict(),
            "eie": self.eie.to_dict(),
            "pnl_delta_usd": round(self.pnl_delta_usd, 4),
            "eie_better": self.eie_better,
        }


def _direction(side: str) -> float:
    return 1.0 if side.upper() == "LONG" else -1.0


def _pnl(
    side: str, entry: float, exit_px: float, size_usd: float
) -> tuple[float, float]:
    d = _direction(side)
    pct = ((exit_px - entry) / entry) * d if entry > 0 else 0.0
    return size_usd * pct, pct * 100.0


def _r_multiple(side: str, entry: float, exit_px: float, stop: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    d = _direction(side)
    return ((exit_px - entry) * d) / risk


class ExitReplayEngine:
    """Replay a single scenario under a chosen policy."""

    def __init__(
        self,
        *,
        position_manager: PositionManager | None = None,
        eie_evaluate_every_n_bars: int = 1,
    ) -> None:
        self._pm = position_manager or PositionManager()
        self._eie_every = max(1, eie_evaluate_every_n_bars)

    def run_static(
        self, scenario: TradeScenario, bars: Sequence[PriceBar]
    ) -> ExitPolicyResult:
        """Classic fixed TP/SL (optional trailing / breakeven)."""
        side = scenario.side.upper()
        is_long = side == "LONG"
        entry = scenario.entry_price
        sl = scenario.stop_loss
        tps = list(scenario.take_profit_levels)
        size = scenario.position_size_usd
        r_dist = abs(entry - sl)
        mfe = 0.0
        mae = 0.0
        partials = 0
        realized = 0.0
        remaining_size = size
        notes: list[str] = []

        for i, bar in enumerate(bars):
            # Conservative intra-bar: check adverse extreme first
            adverse = bar.low if is_long else bar.high
            favorable = bar.high if is_long else bar.low

            # MAE / MFE tracking on close path
            close_move = ((bar.close - entry) / entry) * _direction(side) * 100.0
            mfe = max(mfe, close_move)
            mae = min(mae, close_move)

            # Stop hit on adverse extreme
            sl_hit = adverse <= sl if is_long else adverse >= sl
            if sl_hit:
                px = sl
                pnl, pct = _pnl(side, entry, px, remaining_size)
                return ExitPolicyResult(
                    policy="static",
                    exit_price=px,
                    exit_reason="sl_triggered",
                    exit_bar_index=i,
                    hold_bars=i + 1,
                    pnl_usd=round(realized + pnl, 4),
                    pnl_pct=round(
                        ((realized + pnl) / size) * 100.0 if size else 0.0, 4
                    ),
                    r_multiple=round(
                        _r_multiple(side, entry, px, scenario.stop_loss), 4
                    ),
                    max_favorable_pct=round(mfe, 4),
                    max_adverse_pct=round(mae, 4),
                    partial_exits=partials,
                    notes=tuple(notes),
                )

            # TP levels
            if tps:
                next_tp = tps[0]
                tp_hit = favorable >= next_tp if is_long else favorable <= next_tp
                if tp_hit:
                    if len(tps) > 1:
                        # Partial at 50%
                        frac = 0.5
                        part_pnl, _ = _pnl(side, entry, next_tp, remaining_size * frac)
                        realized += part_pnl
                        remaining_size *= 1.0 - frac
                        partials += 1
                        tps.pop(0)
                        notes.append(f"partial@{next_tp}")
                        # Move SL to breakeven optionally
                        if scenario.breakeven_at_r is not None:
                            sl = entry
                        continue
                    px = next_tp
                    pnl, pct = _pnl(side, entry, px, remaining_size)
                    return ExitPolicyResult(
                        policy="static",
                        exit_price=px,
                        exit_reason="tp_triggered",
                        exit_bar_index=i,
                        hold_bars=i + 1,
                        pnl_usd=round(realized + pnl, 4),
                        pnl_pct=round(
                            ((realized + pnl) / size) * 100.0 if size else 0.0, 4
                        ),
                        r_multiple=round(
                            _r_multiple(side, entry, px, scenario.stop_loss), 4
                        ),
                        max_favorable_pct=round(mfe, 4),
                        max_adverse_pct=round(mae, 4),
                        partial_exits=partials,
                        notes=tuple(notes),
                    )

            # Trailing
            if scenario.trailing_sl and r_dist > 0:
                candidate = bar.close - r_dist if is_long else bar.close + r_dist
                if is_long and candidate > sl or not is_long and candidate < sl:
                    sl = candidate

            # Breakeven
            if (
                scenario.breakeven_at_r is not None
                and r_dist > 0
                and _r_multiple(side, entry, bar.close, scenario.stop_loss)
                >= scenario.breakeven_at_r
            ):
                if is_long and sl < entry or not is_long and sl > entry:
                    sl = entry

        # Path exhausted — exit at last close
        last = bars[-1] if bars else None
        px = last.close if last else entry
        pnl, pct = _pnl(side, entry, px, remaining_size)
        return ExitPolicyResult(
            policy="static",
            exit_price=px,
            exit_reason="path_end",
            exit_bar_index=max(0, len(bars) - 1),
            hold_bars=len(bars),
            pnl_usd=round(realized + pnl, 4),
            pnl_pct=round(((realized + pnl) / size) * 100.0 if size else 0.0, 4),
            r_multiple=round(_r_multiple(side, entry, px, scenario.stop_loss), 4),
            max_favorable_pct=round(mfe, 4),
            max_adverse_pct=round(mae, 4),
            partial_exits=partials,
            notes=tuple(notes + ["path_exhausted"]),
        )

    def run_eie(
        self, scenario: TradeScenario, bars: Sequence[PriceBar]
    ) -> ExitPolicyResult:
        """Exit Intelligence policy via PositionManager."""
        side = scenario.side.upper()
        is_long = side == "LONG"
        entry = scenario.entry_price
        sl = scenario.stop_loss
        size = scenario.position_size_usd
        qty = scenario.resolved_quantity()
        mfe = 0.0
        mae = 0.0
        partials = 0
        realized = 0.0
        remaining_size = size
        remaining_qty = qty
        notes: list[str] = []

        trade = Trade(
            trade_id="eval-trade",
            symbol=scenario.symbol,
            side=TradeSide.LONG if is_long else TradeSide.SHORT,
            entry_price=entry,
            quantity=qty,
            leverage=1.0,
            position_size_usd=size,
            risk_amount_usd=(
                abs(entry - scenario.stop_loss) / entry * size if entry else 0.0
            ),
            strategy_id=scenario.strategy_id,
            agent_consensus={},
            explanation="offline_eval",
            sl_price=sl,
            tp_price=(
                scenario.take_profit_levels[0] if scenario.take_profit_levels else entry
            ),
            take_profit_levels=list(scenario.take_profit_levels),
            state=TradeLifecycleState.POSITION_OPENED,
            entry_time=(
                bars[0].timestamp.isoformat()
                if bars
                else datetime.now(timezone.utc).isoformat()
            ),
        )

        for i, bar in enumerate(bars):
            close_move = ((bar.close - entry) / entry) * _direction(side) * 100.0
            mfe = max(mfe, close_move)
            mae = min(mae, close_move)

            # Hard SL on adverse extreme (always authoritative)
            adverse = bar.low if is_long else bar.high
            if (is_long and adverse <= trade.sl_price) or (
                not is_long and adverse >= trade.sl_price
            ):
                px = trade.sl_price
                pnl, _ = _pnl(side, entry, px, remaining_size)
                return ExitPolicyResult(
                    policy="eie",
                    exit_price=px,
                    exit_reason="sl_triggered",
                    exit_bar_index=i,
                    hold_bars=i + 1,
                    pnl_usd=round(realized + pnl, 4),
                    pnl_pct=round(
                        ((realized + pnl) / size) * 100.0 if size else 0.0, 4
                    ),
                    r_multiple=round(
                        _r_multiple(side, entry, px, scenario.stop_loss), 4
                    ),
                    max_favorable_pct=round(mfe, 4),
                    max_adverse_pct=round(mae, 4),
                    partial_exits=partials,
                    notes=tuple(notes),
                )

            # EIE evaluation on a cadence
            if i % self._eie_every == 0:
                # Simple trend proxy from recent closes
                lookback = bars[max(0, i - 20) : i + 1]
                if len(lookback) >= 2:
                    ret = (lookback[-1].close - lookback[0].close) / lookback[0].close
                    trend_strength = max(0.0, min(1.0, 0.5 + ret * 10.0))
                else:
                    trend_strength = 0.5

                # Rough ATR from recent ranges
                atr = None
                if len(lookback) >= 5:
                    ranges = [b.high - b.low for b in lookback[-14:]]
                    atr = sum(ranges) / len(ranges)

                action = self._pm.evaluate(
                    trade=trade,
                    current_price=bar.close,
                    trend_strength=trend_strength,
                    atr=atr,
                    swing_lows=[scenario.stop_loss] if is_long else [],
                    swing_highs=[scenario.stop_loss] if not is_long else [],
                    prior_highs=list(scenario.take_profit_levels) if is_long else [],
                    prior_lows=list(scenario.take_profit_levels) if not is_long else [],
                    timestamp=bar.timestamp,
                )

                if action.action == ExitAction.EXIT:
                    px = bar.close
                    pnl, _ = _pnl(side, entry, px, remaining_size)
                    notes.append(action.reason)
                    return ExitPolicyResult(
                        policy="eie",
                        exit_price=px,
                        exit_reason=f"eie_exit:{action.reason[:80]}",
                        exit_bar_index=i,
                        hold_bars=i + 1,
                        pnl_usd=round(realized + pnl, 4),
                        pnl_pct=round(
                            ((realized + pnl) / size) * 100.0 if size else 0.0, 4
                        ),
                        r_multiple=round(
                            _r_multiple(side, entry, px, scenario.stop_loss), 4
                        ),
                        max_favorable_pct=round(mfe, 4),
                        max_adverse_pct=round(mae, 4),
                        partial_exits=partials,
                        notes=tuple(notes),
                    )

                if action.action == ExitAction.MANAGE:
                    if action.reduce_fraction > 0 and remaining_size > 0:
                        frac = min(action.reduce_fraction, 1.0)
                        part_pnl, _ = _pnl(
                            side, entry, bar.close, remaining_size * frac
                        )
                        realized += part_pnl
                        remaining_size *= 1.0 - frac
                        remaining_qty *= 1.0 - frac
                        trade.position_size_usd = remaining_size
                        trade.quantity = remaining_qty
                        partials += 1
                        notes.append(f"manage_reduce@{frac:.2f}")
                    if action.new_stop_price is not None:
                        trade.sl_price = action.new_stop_price
                        notes.append(f"tighten_sl@{action.new_stop_price}")

        last = bars[-1] if bars else None
        px = last.close if last else entry
        pnl, _ = _pnl(side, entry, px, remaining_size)
        return ExitPolicyResult(
            policy="eie",
            exit_price=px,
            exit_reason="path_end",
            exit_bar_index=max(0, len(bars) - 1),
            hold_bars=len(bars),
            pnl_usd=round(realized + pnl, 4),
            pnl_pct=round(((realized + pnl) / size) * 100.0 if size else 0.0, 4),
            r_multiple=round(_r_multiple(side, entry, px, scenario.stop_loss), 4),
            max_favorable_pct=round(mfe, 4),
            max_adverse_pct=round(mae, 4),
            partial_exits=partials,
            notes=tuple(notes + ["path_exhausted"]),
        )


def compare_policies(
    scenario: TradeScenario,
    bars: Sequence[PriceBar],
    *,
    position_manager: PositionManager | None = None,
    eie_evaluate_every_n_bars: int = 1,
) -> ExitReplaySummary:
    """Run static and eie policies on the same path and return a summary."""
    if not bars:
        raise ValueError("bars must be non-empty")
    engine = ExitReplayEngine(
        position_manager=position_manager,
        eie_evaluate_every_n_bars=eie_evaluate_every_n_bars,
    )
    static = engine.run_static(scenario, bars)
    eie = engine.run_eie(scenario, bars)
    return ExitReplaySummary(
        scenario=scenario,
        static=static,
        eie=eie,
        bars_consumed=len(bars),
    )
