from __future__ import annotations

import pytest

from aitos.execution.binance_account_cost_providers import (
    BinanceUsdMAccountCostProviders,
)


class _FakeBinanceExecutor:
    def __init__(self, shallow=False):
        self.shallow = shallow
        self.calls = []

    async def _signed_request(self, method, path, params):
        self.calls.append((method, path, params))
        if path == "/fapi/v1/commissionRate":
            return {"takerCommissionRate": "0.0004"}
        if path == "/fapi/v1/premiumIndex":
            return {"lastFundingRate": "0.0001"}
        if path == "/fapi/v1/fundingInfo":
            return [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}]
        if path == "/fapi/v1/depth":
            if self.shallow:
                return {"bids": [["99.9", "0.001"]], "asks": [["100.1", "0.001"]]}
            return {
                "bids": [["99.9", "100"], ["99.8", "100"]],
                "asks": [["100.1", "100"], ["100.2", "100"]],
            }
        raise AssertionError(path)


@pytest.mark.asyncio
async def test_authenticated_binance_cost_providers_wire_api_data():
    executor = _FakeBinanceExecutor()
    providers = BinanceUsdMAccountCostProviders(
        executor,
        liquidation_distance_provider=lambda **_: {"distance_pct": 4.5},
    )
    adapter = providers.build_adapter()
    quote = await adapter(
        symbol="BTCUSDT",
        side="LONG",
        notional_usd=1000,
        leverage=3,
        holding_horizon_seconds=4 * 3600,
    )

    assert quote.round_trip_fee_bps == pytest.approx(8.0)
    assert quote.funding_cost_bps == pytest.approx(1.0)
    assert quote.expected_slippage_bps > 0
    assert quote.liquidation_distance_pct == pytest.approx(4.5)
    assert {call[1] for call in executor.calls} == {
        "/fapi/v1/commissionRate",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/fundingInfo",
        "/fapi/v1/depth",
    }


@pytest.mark.asyncio
async def test_binance_cost_provider_fails_closed_when_depth_is_insufficient():
    executor = _FakeBinanceExecutor(shallow=True)
    providers = BinanceUsdMAccountCostProviders(
        executor,
        liquidation_distance_provider=lambda **_: {"distance_pct": 4.5},
    )
    with pytest.raises(RuntimeError, match="depth is insufficient"):
        await providers.depth_slippage(symbol="BTCUSDT", side="LONG", notional_usd=1000)
