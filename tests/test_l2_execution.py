from aitos.backtest.l2_execution import BookLevel, L2ExecutionModel


def test_buy_consumes_asks_and_reports_partial_fill():
    model = L2ExecutionModel()
    result = model.execute(
        "buy", 3.0, [BookLevel(99, 10)], [BookLevel(100, 1), BookLevel(101, 2)]
    )
    assert result.filled_quantity == 3.0
    assert result.remaining_quantity == 0.0
    assert result.average_price == (100 + 202) / 3


def test_sell_consumes_bids_and_reports_unfilled_quantity():
    model = L2ExecutionModel()
    result = model.execute("sell", 3.0, [BookLevel(99, 2)], [BookLevel(100, 10)])
    assert result.filled_quantity == 2.0
    assert result.remaining_quantity == 1.0
    assert result.average_price == 99


def test_max_levels_limits_visible_depth():
    model = L2ExecutionModel(max_levels=1)
    result = model.execute("buy", 2.0, [], [BookLevel(100, 1), BookLevel(101, 5)])
    assert result.filled_quantity == 1.0
    assert result.remaining_quantity == 1.0
