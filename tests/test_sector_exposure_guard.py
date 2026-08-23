import pytest

from aitos.risk.models import PortfolioState, PositionExposure, RiskLimits
from aitos.risk.position_sizing import calculate_position_size
from aitos.risk.risk_engine import check_limits
from aitos.risk.sector import sector_for_symbol


def test_known_symbols_are_classified_without_unclassified_bucket():
    assert sector_for_symbol("BNBUSDT") == "exchange-token"
    assert sector_for_symbol("SOLUSDT") == "layer1"
    assert sector_for_symbol("LINKUSDT") == "oracle-infrastructure"
    assert sector_for_symbol("UNKNOWNUSDT") == "other"


def test_position_exposure_auto_classifies_symbol():
    position = PositionExposure(symbol="BNBUSDT", notional_usd=1000.0, leverage=10.0)
    assert position.sector == "exchange-token"
    assert position.sector != "unclassified"


def test_sector_cap_is_reported_as_default_limit_breach():
    portfolio = PortfolioState(
        equity_usd=10_000.0,
        peak_equity_usd=10_000.0,
        positions=(
            PositionExposure(symbol="BNBUSDT", notional_usd=2500.0, leverage=10.0),
        ),
    )
    breaches = check_limits(portfolio, RiskLimits())
    sector_breach = next(
        b for b in breaches if b.limit_name == "max_sector_exposure_pct[exchange-token]"
    )
    assert sector_breach.observed_value == pytest.approx(25.0)
    assert sector_breach.is_hard_cap is False
    assert sector_breach.limit_value == pytest.approx(20.0)
    assert "default limit" in sector_breach.message


def test_position_sizing_can_cap_projected_sector_notional():
    result = calculate_position_size(
        equity_usd=10_000.0,
        entry_price=100.0,
        stop_loss_price=99.0,
        risk_limits=RiskLimits(),
        volatility_percentile=0.0,
        sector_limit_pct=20.0,
        existing_sector_notional_usd=1500.0,
    )
    assert result.position_size_usd == pytest.approx(500.0)
    assert "sector_notional_cap=2000.00" in result.rationale
