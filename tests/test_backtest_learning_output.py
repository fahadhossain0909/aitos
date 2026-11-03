from datetime import datetime, timezone

from aitos.backtest.cli import HistoricalEvent
from aitos.backtest.engine import BacktestEngine


def test_backtest_result_contains_realized_trade_outcome():
    events = [
        HistoricalEvent(
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            100.0,
            {"symbol": "BTCUSDT", "trend_strength": 8},
        ),
        HistoricalEvent(
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            110.0,
            {"symbol": "BTCUSDT", "trend_strength": 9},
        ),
    ]

    def strategy(event, execution):
        if not getattr(execution, "entered", False):
            execution.execute("buy", 1.0, event.price)
            execution.entered = True
        elif getattr(execution, "position_qty", 0.0) > 0:
            execution.execute("sell", 1.0, event.price)

    result = BacktestEngine(10_000.0).run(events, strategy, lambda event: event.price)
    assert len(result.trades) == 1
    assert result.trades[0].reward > 0
    assert result.trades[0].fields["symbol"] == "BTCUSDT"
