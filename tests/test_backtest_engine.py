from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aitos.backtest.engine import BacktestEngine


@dataclass(frozen=True)
class Event:
    timestamp: datetime
    price: float


def test_backtest_engine_records_equity_and_metrics():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        Event(t, 100.0),
        Event(t + timedelta(seconds=1), 110.0),
        Event(t + timedelta(seconds=2), 105.0),
    ]

    def strategy(event, execution):
        if event.price == 100.0:
            execution.execute("buy", 1.0, event.price)
        elif event.price == 110.0:
            execution.execute("sell", 1.0, event.price)

    result = BacktestEngine(1000.0, fee_rate=0.0).run(
        events, strategy, lambda event: event.price
    )
    assert len(result.equity_curve) == 3
    assert result.metrics.final_equity == 1010.0
    assert result.metrics.total_return == 0.01
    assert result.metrics.trades == 1
    assert result.metrics.wins == 1
    assert result.metrics.win_rate == 1.0
    assert result.metrics.total_fees == 0.0
