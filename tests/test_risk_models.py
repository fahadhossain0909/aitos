import pytest
from pydantic import ValidationError

from aitos.risk.models import PortfolioState, PositionExposure, RiskLimits


def test_risk_limits_defaults_match_spec_table():
    limits = RiskLimits()
    assert limits.max_risk_per_trade_pct == 1.0
    assert limits.max_risk_per_trade_hard_cap_pct == 2.0
    assert limits.max_drawdown_pct == 10.0
    assert limits.max_drawdown_hard_cap_pct == 20.0
    assert limits.max_leverage == 10.0
    assert limits.max_leverage_hard_cap == 125.0
    assert limits.max_open_positions == 10
    assert limits.max_open_positions_hard_cap == 20


def test_risk_limits_default_exceeding_hard_cap_raises():
    with pytest.raises(ValidationError):
        RiskLimits(max_drawdown_pct=25.0, max_drawdown_hard_cap_pct=20.0)


def test_risk_limits_hard_cap_over_125_leverage_still_valid_if_default_lower():
    limits = RiskLimits(max_leverage=20.0, max_leverage_hard_cap=125.0)
    assert limits.max_leverage == 20.0


def test_portfolio_state_current_drawdown_pct():
    portfolio = PortfolioState(equity_usd=9000.0, peak_equity_usd=10000.0)
    assert portfolio.current_drawdown_pct == pytest.approx(10.0)


def test_portfolio_state_no_drawdown_when_at_peak():
    portfolio = PortfolioState(equity_usd=10000.0, peak_equity_usd=10000.0)
    assert portfolio.current_drawdown_pct == 0.0


def test_portfolio_state_gross_exposure_and_max_leverage():
    positions = (
        PositionExposure(
            symbol="BTCUSDT", notional_usd=5000.0, leverage=5.0, sector="crypto-major"
        ),
        PositionExposure(
            symbol="ETHUSDT", notional_usd=3000.0, leverage=8.0, sector="crypto-major"
        ),
    )
    portfolio = PortfolioState(
        equity_usd=10000.0, peak_equity_usd=10000.0, positions=positions
    )
    assert portfolio.gross_exposure_usd == 8000.0
    assert portfolio.max_position_leverage == 8.0


def test_portfolio_state_sector_exposure_pct():
    positions = (
        PositionExposure(
            symbol="BTCUSDT", notional_usd=2000.0, leverage=5.0, sector="crypto-major"
        ),
        PositionExposure(
            symbol="LINKUSDT", notional_usd=1000.0, leverage=5.0, sector="crypto-alt"
        ),
    )
    portfolio = PortfolioState(
        equity_usd=10000.0, peak_equity_usd=10000.0, positions=positions
    )
    exposures = portfolio.sector_exposure_pct
    assert exposures["crypto-major"] == pytest.approx(20.0)
    assert exposures["crypto-alt"] == pytest.approx(10.0)
