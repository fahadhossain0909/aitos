"""Runtime guards for bounded open-position analysis.

The scanner's candidate universe is intentionally dynamic, but an open trade is
never allowed to disappear from the market-data universe merely because it fell
out of the scanner ranking.  This module keeps those concerns separate and
places a hard five-position ceiling at the TradeLifecycle boundary.

The expensive order-book universe is bounded to BTC plus the currently open
positions, with remaining capacity available to scanner candidates.  Thus a
portfolio with five positions uses at most six deep symbols (BTC reference +
five positions), while a portfolio with fewer positions can still analyze new
candidates.
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import replace
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from aitos.data.ingestion import DataIngestionService
from aitos.models.trade import Opportunity, Trade, TradeLifecycleState
from aitos.trading.lifecycle import TradeLifecycle

MAX_OPEN_POSITIONS = 5
POSITION_DATA_RESERVE_PCT = 20.0
POSITION_CAPITAL_POOL_PCT = 100.0 - POSITION_DATA_RESERVE_PCT
MAX_DEEP_SYMBOLS = MAX_OPEN_POSITIONS + 1  # BTC reference + five positions
MAX_SCANNER_DEEP_CANDIDATES = 2
REFERENCE_SYMBOL = "BTCUSDT"

_LIFECYCLES: weakref.WeakSet[TradeLifecycle] = weakref.WeakSet()
_INGESTIONS: weakref.WeakSet[DataIngestionService] = weakref.WeakSet()


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


def _rejected_trade(opportunity: Opportunity, reason: str) -> Trade:
    now = datetime.now(timezone.utc).isoformat()
    return Trade(
        trade_id=f"position-guard-reject-{opportunity.opportunity_id}",
        symbol=opportunity.symbol,
        side=opportunity.side,
        entry_price=opportunity.entry_price,
        quantity=0.0,
        leverage=1.0,
        position_size_usd=0.0,
        risk_amount_usd=0.0,
        strategy_id=opportunity.strategy_id,
        agent_consensus=dict(opportunity.agent_consensus),
        explanation=opportunity.rationale,
        sl_price=opportunity.stop_loss_price,
        tp_price=(
            opportunity.take_profit_levels[0]
            if opportunity.take_profit_levels
            else opportunity.entry_price
        ),
        state=TradeLifecycleState.REJECTED,
        entry_time=now,
        take_profit_levels=list(opportunity.take_profit_levels),
        regime=opportunity.regime,
        rejection_reason=reason,
    )


def _capital_policy_consensus(portfolio: Any) -> dict[str, Any]:
    equity = float(getattr(portfolio, "equity_usd", 0.0) or 0.0)
    deployed = sum(
        float(getattr(position, "notional_usd", 0.0) or 0.0)
        for position in (getattr(portfolio, "positions", ()) or ())
    )
    # This is a capital *policy budget*, not a replacement for stop-risk sizing.
    # It is exposed to the downstream lifecycle so sizing/telemetry can make the
    # five-slot + reserve decision explicit without changing account equity.
    deploy_capital = max(0.0, equity * POSITION_CAPITAL_POOL_PCT / 100.0)
    return {
        "max_open_positions": MAX_OPEN_POSITIONS,
        "open_position_count": len(getattr(portfolio, "positions", ()) or ()),
        "capital_pool_pct": POSITION_CAPITAL_POOL_PCT,
        "reserve_buffer_pct": POSITION_DATA_RESERVE_PCT,
        "capital_pool_usd": round(deploy_capital, 8),
        "deployed_notional_usd": round(deployed, 8),
        "remaining_policy_capacity_usd": round(max(0.0, deploy_capital - deployed), 8),
        "per_slot_capital_usd": round(deploy_capital / MAX_OPEN_POSITIONS, 8),
    }


def _merge_symbols(
    requested: list[str], open_symbols: list[str], *, limit: int | None = None
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for symbol in [*requested, *open_symbols]:
        normalized = str(symbol).upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result if limit is None else result[:limit]


def _reconfigure_deep_sync_marker(ingestion: DataIngestionService) -> None:
    # No-op marker used only to keep the wrapper small and make the policy
    # discoverable in tracebacks/telemetry.
    ingestion._aitos_position_universe_enabled = True


def _install_ingestion_guards() -> None:
    if getattr(DataIngestionService, "_aitos_position_runtime_installed", False):
        return

    original_init = DataIngestionService.__init__
    original_trade = DataIngestionService.update_live_trade_symbols
    original_kline = DataIngestionService.update_live_kline_symbols
    original_book = DataIngestionService.update_live_deep_orderbooks

    @wraps(original_init)
    def guarded_init(self: DataIngestionService, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _INGESTIONS.add(self)
        _reconfigure_deep_sync_marker(self)

    async def guarded_trade(
        self: DataIngestionService, symbols: list[str] | tuple[str, ...]
    ) -> bool:
        merged = _merge_symbols(list(symbols), _open_symbols())
        return await original_trade(self, merged)

    async def guarded_kline(
        self: DataIngestionService, symbols: list[str] | tuple[str, ...]
    ) -> bool:
        merged = _merge_symbols(list(symbols), _open_symbols())
        return await original_kline(self, merged)

    async def guarded_book(
        self: DataIngestionService,
        ranked_non_btc_symbols: list[str] | tuple[str, ...],
    ) -> bool:
        open_symbols = _open_symbols()
        open_non_btc = [s for s in open_symbols if s != REFERENCE_SYMBOL]
        requested = [
            str(s).upper()
            for s in ranked_non_btc_symbols
            if s and str(s).upper() != REFERENCE_SYMBOL
        ]
        # Open positions have priority. Remaining expensive-data capacity is
        # offered to scanner candidates. BTC remains the shared reference.
        non_btc: list[str] = []
        seen: set[str] = set()
        for symbol in [*open_non_btc, *requested]:
            if symbol in seen:
                continue
            seen.add(symbol)
            non_btc.append(symbol)
        non_btc = non_btc[:MAX_OPEN_POSITIONS]
        symbols = [REFERENCE_SYMBOL, *non_btc]
        if REFERENCE_SYMBOL not in {s.upper() for s in getattr(self, "_symbols", ())}:
            symbols = non_btc[:MAX_DEEP_SYMBOLS]
        return (
            await self._canonical_runtime.update_orderbook_symbols(
                symbols[:MAX_DEEP_SYMBOLS]
            )
            if self._canonical_runtime is not None
            else False
        )

    DataIngestionService.__init__ = guarded_init  # type: ignore[method-assign]
    DataIngestionService.update_live_trade_symbols = guarded_trade  # type: ignore[method-assign]
    DataIngestionService.update_live_kline_symbols = guarded_kline  # type: ignore[method-assign]
    DataIngestionService.update_live_deep_orderbooks = guarded_book  # type: ignore[method-assign]
    DataIngestionService._aitos_position_runtime_installed = True  # type: ignore[attr-defined]


def _install_lifecycle_guard() -> None:
    if getattr(TradeLifecycle, "_aitos_position_runtime_installed", False):
        return

    original_init = TradeLifecycle.__init__
    original_submit = TradeLifecycle.submit_opportunity

    @wraps(original_init)
    def guarded_init(self: TradeLifecycle, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _LIFECYCLES.add(self)
        self._aitos_position_guard_lock = asyncio.Lock()

    @wraps(original_submit)
    async def guarded_submit(
        self: TradeLifecycle,
        opportunity: Opportunity,
        portfolio: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Trade:
        lock = getattr(self, "_aitos_position_guard_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._aitos_position_guard_lock = lock
        async with lock:
            positions = tuple(getattr(portfolio, "positions", ()) or ())
            open_symbols = {
                str(getattr(position, "symbol", "")).upper()
                for position in positions
                if getattr(position, "symbol", "")
            }
            if str(opportunity.symbol).upper() in open_symbols:
                return _rejected_trade(
                    opportunity,
                    "position_guard: symbol already has an open position",
                )
            if len(open_symbols) >= MAX_OPEN_POSITIONS:
                return _rejected_trade(
                    opportunity,
                    f"position_guard: hard maximum of {MAX_OPEN_POSITIONS} open positions reached",
                )
            consensus = dict(opportunity.agent_consensus)
            consensus["position_portfolio_policy"] = _capital_policy_consensus(
                portfolio
            )
            protected = replace(opportunity, agent_consensus=consensus)
            trade = await original_submit(self, protected, portfolio, *args, **kwargs)
            if trade.state == TradeLifecycleState.POSITION_OPENED:
                # Reconfigure every live ingestion instance immediately. This is
                # deliberately independent of scanner rank so the new position
                # cannot become data-blind after the next scan.
                for ingestion in list(_INGESTIONS):
                    try:
                        current = list(getattr(ingestion, "_live_trade_symbols", ()))
                        await guarded_trade(ingestion, current)
                        current_k = list(getattr(ingestion, "_live_kline_symbols", ()))
                        await guarded_kline(ingestion, current_k)
                        current_b = (
                            list(
                                getattr(
                                    ingestion, "_canonical_runtime", None
                                ).orderbook_symbols
                            )
                            if getattr(ingestion, "_canonical_runtime", None)
                            is not None
                            else []
                        )
                        await guarded_book(ingestion, current_b)
                    except Exception:
                        # Market-data reconfiguration must not corrupt the trade
                        # lifecycle; the next scanner stage will retry it.
                        continue
            return trade

    TradeLifecycle.__init__ = guarded_init  # type: ignore[method-assign]
    TradeLifecycle.submit_opportunity = guarded_submit  # type: ignore[method-assign]
    TradeLifecycle._aitos_position_runtime_installed = True  # type: ignore[attr-defined]


_install_ingestion_guards()
_install_lifecycle_guard()
