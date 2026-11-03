from aitos.backtest.execution import ExecutionSimulator


def test_buy_sell_round_trip_realizes_pnl_and_fees():
    sim = ExecutionSimulator(10_000, fee_rate=0.001, slippage_bps=10)
    sim.execute("buy", 1.0, 100.0)
    sim.execute("sell", 1.0, 110.0)
    snap = sim.snapshot(110.0)
    assert snap.position_qty == 0
    assert snap.realized_pnl > 0
    assert snap.fees > 0
    assert snap.equity < 10_010


def test_partial_close_keeps_remaining_entry():
    sim = ExecutionSimulator(10_000)
    sim.execute("buy", 2.0, 100.0)
    sim.execute("sell", 1.0, 110.0)
    snap = sim.snapshot(110.0)
    assert snap.position_qty == 1.0
    assert snap.avg_entry == 100.0
    assert snap.realized_pnl > 0
