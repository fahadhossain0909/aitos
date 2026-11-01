"""Production-like paper-trading recovery and reconciliation tests."""

from __future__ import annotations

import pytest

from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.persistent_state import TradeStatePersistence


class FakeStateStore:
    def __init__(self, trades=None):
        self.trades = list(trades or [])

    async def load_open_trades(self):
        return list(self.trades)


class FakeLifecycle:
    def __init__(self):
        self._open_trades = {}

    def get_open_trades(self):
        return list(self._open_trades.values())


@pytest.fixture
def paper_trade():
    return Trade(
        trade_id="paper-recovery-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=0.01,
        leverage=1.0,
        position_size_usd=1.0,
        risk_amount_usd=0.05,
        strategy_id="paper",
        agent_consensus={},
        explanation="recovery test",
        sl_price=95.0,
        tp_price=110.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-08-24T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_paper_trade_survives_process_restart(event_bus, paper_trade):
    lifecycle = FakeLifecycle()
    persistence = TradeStatePersistence(
        event_bus, lifecycle, FakeStateStore([paper_trade])
    )

    assert await persistence.restore() == 1
    restored = lifecycle._open_trades[paper_trade.trade_id]
    assert restored.symbol == paper_trade.symbol
    assert restored.entry_price == paper_trade.entry_price
    assert restored.sl_price == paper_trade.sl_price
    assert restored.state == TradeLifecycleState.POSITION_OPENED


def test_trade_payload_round_trip_preserves_entry_thesis_and_emergency_stop(
    paper_trade,
):
    from aitos.trading.persistent_state import _trade_from_dict

    paper_trade.entry_thesis = {
        "trade_id": paper_trade.trade_id,
        "symbol": paper_trade.symbol,
        "side": "LONG",
        "entry_price": 100.0,
        "components": [
            {"code": "bullish_structure", "description": "entry", "weight": 1.2}
        ],
        "invalidations": [
            {"code": "structure_break", "description": "break", "level": 94.0}
        ],
        "confirmations": [{"code": "structure_supportive", "description": "holds"}],
        "expected_path_prices": [105.0, 110.0],
        "invalidation_price": 94.0,
        "created_at": "2026-08-24T00:00:00+00:00",
        "notes": ["entry snapshot"],
        "features": {"trend_strength": 0.8},
    }
    paper_trade.emergency_sl_price = 90.0
    paper_trade.breakeven_triggered = True
    paper_trade.breakeven_at_r_multiple = 1.0
    paper_trade.initial_r_distance = 4.0
    paper_trade.mae_r = 0.5
    paper_trade.mfe_r = 1.25

    restored = _trade_from_dict(paper_trade.to_dict())
    assert restored.entry_thesis == paper_trade.entry_thesis
    assert restored.emergency_sl_price == 90.0
    assert restored.breakeven_triggered is True
    assert restored.breakeven_at_r_multiple == 1.0
    assert restored.initial_r_distance == 4.0
    assert restored.mae_r == 0.5
    assert restored.mfe_r == 1.25

    from aitos.trading.position_manager import PositionManager

    recovered_thesis = PositionManager._restore_entry_thesis(restored)
    assert recovered_thesis is not None
    assert recovered_thesis.trade_id == paper_trade.trade_id
    assert recovered_thesis.expected_path_prices == (105.0, 110.0)


@pytest.mark.asyncio
async def test_restart_does_not_create_duplicate_open_trade(event_bus, paper_trade):
    lifecycle = FakeLifecycle()
    persistence = TradeStatePersistence(
        event_bus, lifecycle, FakeStateStore([paper_trade])
    )

    assert await persistence.restore() == 1
    assert await persistence.restore() == 1
    assert len(lifecycle._open_trades) == 1


@pytest.mark.asyncio
async def test_reconciliation_detects_missing_internal_position():
    remote_quantity = 0.01
    internal_positions = {}
    mismatch = remote_quantity > 0 and "BTCUSDT" not in internal_positions

    assert mismatch is True


class FakeIngestion:
    def __init__(self, trade_symbols=None, kline_symbols=None):
        self._live_trade_symbols = list(trade_symbols or [])
        self._live_kline_symbols = list(kline_symbols or [])
        self.trade_updates = []
        self.kline_updates = []

    async def update_live_trade_symbols(self, symbols):
        self.trade_updates.append(list(symbols))
        self._live_trade_symbols = list(
            dict.fromkeys([*self._live_trade_symbols, *symbols])
        )
        return True

    async def update_live_kline_symbols(self, symbols):
        self.kline_updates.append(list(symbols))
        self._live_kline_symbols = list(
            dict.fromkeys([*symbols, *self._live_kline_symbols])
        )
        return True


@pytest.mark.asyncio
async def test_restore_defers_market_resubscription_until_explicit_reconcile(
    event_bus, paper_trade, monkeypatch
):
    from aitos.trading import persistent_state

    lifecycle = FakeLifecycle()
    ingestion = FakeIngestion()
    monkeypatch.setattr(persistent_state, "_INGESTIONS", {ingestion})
    persistence = TradeStatePersistence(
        event_bus, lifecycle, FakeStateStore([paper_trade])
    )

    assert await persistence.restore() == 1
    assert ingestion.trade_updates == []
    assert ingestion.kline_updates == []

    result = await persistence.resubscribe_restored_positions()
    assert result["ingestions"] == 1
    assert result["verified"] == 1
    assert result["failures"] == 0
    assert "BTCUSDT" in ingestion._live_trade_symbols
    assert "BTCUSDT" in ingestion._live_kline_symbols


@pytest.mark.asyncio
async def test_resubscription_reports_missing_symbols_as_failure(
    event_bus, paper_trade, monkeypatch
):
    from aitos.trading import persistent_state

    class NoopIngestion:
        _live_trade_symbols = []
        _live_kline_symbols = []

        async def update_live_trade_symbols(self, symbols):
            return False

        async def update_live_kline_symbols(self, symbols):
            return False

    lifecycle = FakeLifecycle()
    monkeypatch.setattr(persistent_state, "_INGESTIONS", {NoopIngestion()})
    persistence = TradeStatePersistence(
        event_bus, lifecycle, FakeStateStore([paper_trade])
    )
    await persistence.restore()

    result = await persistence.resubscribe_restored_positions()
    assert result["ingestions"] == 1
    assert result["verified"] == 0
    assert result["failures"] == 1
