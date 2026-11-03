from datetime import datetime, timezone

from aitos.backtest.queue_lifecycle import QueueOrderLifecycle, SimulatedOrder


def test_book_reduction_and_trade_can_fill_order():
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lifecycle = QueueOrderLifecycle()
    lifecycle.place(SimulatedOrder("o1", "buy", 100.0, 1.0, 1.0, 2.0, ts))
    lifecycle.on_book_change("buy", 100.0, 3.0, 2.0, ts)
    fills = lifecycle.consume("buy", 100.0, 2.0, ts)
    assert len(fills) == 1
    assert fills[0].order_id == "o1"
    assert fills[0].quantity == 1.0
