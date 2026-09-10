"""Runtime wiring for position-aware market-data coverage and tiered monitoring."""

from __future__ import annotations

import os
import weakref
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from aitos.data.ingestion import DataIngestionService
from aitos.intelligence.exit_intelligence import ExitAction
from aitos.intelligence.position_monitor import (
    PositionMonitorController,
    PositionMonitorTier,
)
from aitos.trading.lifecycle import TradeLifecycle
from aitos.trading.position_manager import PositionAction, PositionManager

REFERENCE_SYMBOL = "BTCUSDT"


def _resource_budget(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(1, value)


# Resource budget only; this is not a trading, sizing, or position-count rule.
MAX_DEEP_SYMBOLS = _resource_budget("AITOS_MAX_DEEP_SYMBOLS", 6)

_LIFECYCLES: weakref.WeakSet[TradeLifecycle] = weakref.WeakSet()
_INGESTIONS: weakref.WeakSet[DataIngestionService] = weakref.WeakSet()
_MONITORS: weakref.WeakKeyDictionary[PositionManager, PositionMonitorController] = (
    weakref.WeakKeyDictionary()
)
_DEEP_PRIORITY_SYMBOLS: dict[str, tuple[PositionMonitorTier, float]] = {}


def _open_symbols() -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for lifecycle in list(_LIFECYCLES):
        try:
            trades = lifecycle.get_open_trades()
        except Exception:
            continue
        for trade in trades:
            symbol = str(getattr(trade, "symbol", "") or "").upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols


def _merge_symbols(requested: list[str], protected: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for symbol in [*requested, *protected]:
        normalized = str(symbol).upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _deep_priority_order(
    priorities: dict[str, tuple[PositionMonitorTier, float]] | None = None,
) -> list[str]:
    """Return escalated symbols in resource urgency order only."""
    source = priorities if priorities is not None else _DEEP_PRIORITY_SYMBOLS
    return [
        symbol
        for symbol, (tier, _priority) in sorted(
            source.items(),
            key=lambda item: (
                -item[1][1],
                -int(item[1][0] == PositionMonitorTier.EXIT_CANDIDATE),
            ),
        )
        if tier in {PositionMonitorTier.WARNING, PositionMonitorTier.EXIT_CANDIDATE}
    ]


def _capital_policy(portfolio: Any) -> dict[str, Any]:
    """Expose monitoring metadata without imposing position-size allocation."""
    positions = tuple(getattr(portfolio, "positions", ()) or ())
    return {
        "open_position_count": len(positions),
        "monitoring_model": "normal_warning_exit_candidate",
        "position_sizing_authority": "existing_position_manager_and_risk_engine",
        "fixed_per_position_capital_pct": None,
        "fixed_per_position_capital_usd": None,
    }


# Backward-compatible test/import name. It intentionally carries metadata only.
_capital_policy_consensus = _capital_policy


def _install_ingestion_guards() -> None:
    if getattr(DataIngestionService, "_aitos_position_runtime_installed", False):
        return
    original_init = DataIngestionService.__init__
    original_trade = DataIngestionService.update_live_trade_symbols
    original_kline = DataIngestionService.update_live_kline_symbols

    @wraps(original_init)
    def guarded_init(self: DataIngestionService, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _INGESTIONS.add(self)
        self._aitos_position_universe_enabled = True

    async def guarded_trade(
        self: DataIngestionService, symbols: list[str] | tuple[str, ...]
    ) -> bool:
        # Open positions are protected before scanner symbols. This matters even
        # when a downstream runtime later applies a finite trade-symbol budget.
        return await original_trade(
            self, _merge_symbols(_open_symbols(), list(symbols))
        )

    async def guarded_kline(
        self: DataIngestionService, symbols: list[str] | tuple[str, ...]
    ) -> bool:
        # DataIngestionService caps live klines at LIVE_KLINE_SYMBOLS. Put the
        # protected Position Universe first so scanner ranking cannot evict an
        # open position from its minimum market-data coverage.
        return await original_kline(
            self, _merge_symbols(_open_symbols(), list(symbols))
        )

    async def guarded_book(
        self: DataIngestionService,
        ranked_non_btc_symbols: list[str] | tuple[str, ...],
    ) -> bool:
        requested = [
            str(s).upper()
            for s in ranked_non_btc_symbols
            if s and str(s).upper() != REFERENCE_SYMBOL
        ]
        escalated = [
            symbol for symbol in _deep_priority_order() if symbol != REFERENCE_SYMBOL
        ]
        non_btc = _merge_symbols(escalated, requested)
        symbols = [REFERENCE_SYMBOL, *non_btc[: max(0, MAX_DEEP_SYMBOLS - 1)]]
        runtime = getattr(self, "_canonical_runtime", None)
        if runtime is None:
            return False
        return await runtime.update_orderbook_symbols(symbols)

    DataIngestionService.__init__ = guarded_init  # type: ignore[method-assign]
    DataIngestionService.update_live_trade_symbols = guarded_trade  # type: ignore[method-assign]
    DataIngestionService.update_live_kline_symbols = guarded_kline  # type: ignore[method-assign]
    DataIngestionService.update_live_deep_orderbooks = guarded_book  # type: ignore[method-assign]
    DataIngestionService._aitos_position_runtime_installed = True  # type: ignore[attr-defined]


def _cheap_position_action(
    tier: PositionMonitorTier,
    score: float,
    reasons: tuple[str, ...],
    market_state: Any = None,
) -> PositionAction:
    # The monitor is a risk-state gate only. Even a stop breach is represented
    # as state here; the existing PositionManager/execution protection remains
    # responsible for the actual exit decision.
    return PositionAction(
        action=ExitAction.MANAGE,
        reason=f"POSITION_MONITOR:{tier.value} score={score:.2f} [{', '.join(reasons)}]",
        market_state=market_state,
        notes=(f"monitor_tier={tier.value}", *reasons),
    )


def _warning_market_state(
    self: PositionManager, *, trade: Any, current_price: float, kwargs: dict[str, Any]
) -> Any:
    try:
        atr = kwargs.get("atr")
        volume_profile = kwargs.get("volume_profile")
        return self._mse.compute(
            symbol=trade.symbol,
            mid_price=current_price,
            order_flow=kwargs.get("order_flow"),
            trend_strength=kwargs.get("trend_strength"),
            atr_pct=(
                (atr / current_price * 100.0) if atr and current_price > 0 else None
            ),
            volume_profile_poc=volume_profile.poc if volume_profile else None,
            value_area_high=volume_profile.vah if volume_profile else None,
            value_area_low=volume_profile.val if volume_profile else None,
            structure_bias_hint=None,
            timestamp=kwargs.get("timestamp") or datetime.now(timezone.utc),
            extra_features=kwargs.get("extra_features"),
        )
    except Exception:
        return None


def _install_position_monitor() -> None:
    if getattr(PositionManager, "_aitos_tiered_monitor_installed", False):
        return
    original_evaluate = PositionManager.evaluate
    original_clear = PositionManager.clear_trade

    def monitor_for(manager: PositionManager) -> PositionMonitorController:
        controller = _MONITORS.get(manager)
        if controller is None:
            controller = PositionMonitorController()
            _MONITORS[manager] = controller
        return controller

    @wraps(original_evaluate)
    def guarded_evaluate(
        self: PositionManager,
        *,
        trade: Any,
        current_price: float,
        extra_features: Any = None,
        **kwargs: Any,
    ) -> PositionAction:
        decision = monitor_for(self).evaluate(
            trade=trade, current_price=current_price, extra_features=extra_features
        )
        symbol = str(getattr(trade, "symbol", "")).upper()
        if decision.tier == PositionMonitorTier.NORMAL:
            _DEEP_PRIORITY_SYMBOLS.pop(symbol, None)
            try:
                trade.record_excursion(current_price)
            except Exception:
                pass
            return _cheap_position_action(
                decision.tier, decision.score, decision.reasons
            )

        _DEEP_PRIORITY_SYMBOLS[symbol] = (decision.tier, decision.priority_score)
        if decision.tier == PositionMonitorTier.WARNING:
            return _cheap_position_action(
                decision.tier,
                decision.score,
                decision.reasons,
                market_state=_warning_market_state(
                    self,
                    trade=trade,
                    current_price=current_price,
                    kwargs={**kwargs, "extra_features": extra_features},
                ),
            )

        # EXIT_CANDIDATE is a risk-state handoff. The monitor does not decide
        # the exit path; the existing PositionManager remains authoritative.
        action = original_evaluate(
            self,
            trade=trade,
            current_price=current_price,
            extra_features=extra_features,
            **kwargs,
        )
        return PositionAction(
            action=action.action,
            reason=(
                f"POSITION_MONITOR:{decision.tier.value} "
                f"priority={decision.priority_score:.2f} "
                f"score={decision.score:.2f}; {action.reason}"
            ),
            reduce_fraction=action.reduce_fraction,
            new_stop_price=action.new_stop_price,
            spike_tp_price=action.spike_tp_price,
            exit_decision=action.exit_decision,
            hedge_decision=action.hedge_decision,
            path_plan=action.path_plan,
            structural_stop=action.structural_stop,
            market_state=action.market_state,
            thesis=action.thesis,
            thesis_eval=action.thesis_eval,
            journey=action.journey,
            notes=(
                f"monitor_tier={decision.tier.value}",
                f"priority_score={decision.priority_score:.2f}",
                *decision.reasons,
                *action.notes,
            ),
        )

    def guarded_clear(
        self: PositionManager, trade_id: str, symbol: str | None = None
    ) -> None:
        original_clear(self, trade_id, symbol=symbol)
        monitor_for(self).clear(trade_id)
        if symbol:
            _DEEP_PRIORITY_SYMBOLS.pop(str(symbol).upper(), None)

    PositionManager.evaluate = guarded_evaluate  # type: ignore[method-assign]
    PositionManager.clear_trade = guarded_clear  # type: ignore[method-assign]
    PositionManager._aitos_tiered_monitor_installed = True  # type: ignore[attr-defined]


def _install_lifecycle_wiring() -> None:
    if getattr(TradeLifecycle, "_aitos_position_runtime_installed", False):
        return
    original_init = TradeLifecycle.__init__

    @wraps(original_init)
    def guarded_init(self: TradeLifecycle, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _LIFECYCLES.add(self)

    TradeLifecycle.__init__ = guarded_init  # type: ignore[method-assign]
    TradeLifecycle._aitos_position_runtime_installed = True  # type: ignore[attr-defined]


_install_ingestion_guards()
_install_lifecycle_wiring()
_install_position_monitor()
