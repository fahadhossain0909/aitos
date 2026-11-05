from datetime import datetime, timedelta, timezone

import pytest

from aitos.execution.cost_intelligence import (
    ExecutionCostQuote,
    VenueExecutionCostRegistry,
)


@pytest.mark.asyncio
async def test_registry_accepts_fresh_venue_quote_and_signed_funding_credit():
    registry = VenueExecutionCostRegistry(max_quote_age_seconds=30.0)
    registry.register(
        "binance",
        lambda **kwargs: ExecutionCostQuote(
            venue="binance",
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            round_trip_fee_bps=4.0,
            expected_slippage_bps=1.5,
            funding_cost_bps=-0.8,
            liquidation_distance_pct=4.2,
            observed_at=datetime.now(timezone.utc).isoformat(),
            valid_for_seconds=20.0,
            source="test-adapter",
        ),
    )
    quote = await registry.quote(
        venue="binance",
        symbol="BTCUSDT",
        side="LONG",
        notional_usd=100.0,
        leverage=2.0,
        holding_horizon_seconds=3600.0,
    )
    assert quote.funding_cost_bps == -0.8
    assert quote.as_candidate_costs()["fee_bps"] == 4.0


@pytest.mark.asyncio
async def test_registry_rejects_stale_venue_quote():
    registry = VenueExecutionCostRegistry(max_quote_age_seconds=30.0)
    registry.register(
        "binance",
        lambda **kwargs: ExecutionCostQuote(
            venue="binance",
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            round_trip_fee_bps=4.0,
            expected_slippage_bps=1.5,
            funding_cost_bps=0.8,
            liquidation_distance_pct=4.2,
            observed_at=(
                datetime.now(timezone.utc) - timedelta(seconds=60)
            ).isoformat(),
            valid_for_seconds=20.0,
            source="test-adapter",
        ),
    )
    with pytest.raises(ValueError, match="stale"):
        await registry.quote(
            venue="binance",
            symbol="BTCUSDT",
            side="LONG",
            notional_usd=100.0,
            leverage=2.0,
            holding_horizon_seconds=3600.0,
        )


@pytest.mark.asyncio
async def test_registry_fails_closed_without_venue_adapter():
    registry = VenueExecutionCostRegistry()
    with pytest.raises(LookupError, match="no execution-cost adapter"):
        await registry.quote(
            venue="unknown",
            symbol="BTCUSDT",
            side="LONG",
            notional_usd=100.0,
            leverage=2.0,
            holding_horizon_seconds=3600.0,
        )
