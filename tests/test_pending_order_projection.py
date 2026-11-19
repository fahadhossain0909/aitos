from aitos.risk.models import PendingOrderExposure, PortfolioState, PositionExposure


def test_projected_position_count_uses_unique_normalized_open_and_pending_symbols():
    portfolio = PortfolioState(
        equity_usd=10_000.0,
        peak_equity_usd=10_000.0,
        positions=(PositionExposure("btcusdt", 1_000.0, 1.0),),
        pending_orders=(
            PendingOrderExposure("entry-1", "BTCUSDT", "LONG", 500.0, 10.0),
            PendingOrderExposure("entry-2", "ETHUSDT", "SHORT", 250.0, 5.0),
        ),
    )

    assert portfolio.projected_open_position_count == 2


def test_projected_position_count_is_zero_for_empty_portfolio():
    portfolio = PortfolioState(equity_usd=10_000.0, peak_equity_usd=10_000.0)

    assert portfolio.projected_open_position_count == 0
