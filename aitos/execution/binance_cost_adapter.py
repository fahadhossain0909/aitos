"""Binance USD-M Futures execution-cost adapter.

The adapter intentionally consumes account/market-data providers rather than
hard-coded fee tiers. Wire these providers to authenticated commission data,
funding-rate history/prediction, depth-based impact estimates, and the
exchange's actual position/margin liquidation fields.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from aitos.execution.cost_intelligence import ExecutionCostQuote


Provider = Callable[..., Any]


async def _resolve(provider: Provider, **kwargs: Any) -> Any:
    value = provider(**kwargs)
    return await value if hasattr(value, "__await__") else value


class BinanceUsdMExecutionCostAdapter:
    venue = "binance"

    def __init__(
        self,
        *,
        taker_fee_rate_provider: Provider,
        funding_rate_provider: Provider,
        depth_slippage_bps_provider: Provider,
        liquidation_distance_provider: Provider,
        source: str = "binance-usdm-live-adapter",
        valid_for_seconds: float = 15.0,
    ) -> None:
        self.taker_fee_rate_provider = taker_fee_rate_provider
        self.funding_rate_provider = funding_rate_provider
        self.depth_slippage_bps_provider = depth_slippage_bps_provider
        self.liquidation_distance_provider = liquidation_distance_provider
        self.source = source
        self.valid_for_seconds = float(valid_for_seconds)
        if not 0 < self.valid_for_seconds <= 30:
            raise ValueError("Binance quote validity must be in (0, 30] seconds")

    async def __call__(
        self,
        *,
        symbol: str,
        side: str,
        notional_usd: float,
        leverage: float,
        holding_horizon_seconds: float,
    ) -> ExecutionCostQuote:
        if notional_usd <= 0 or leverage <= 0 or holding_horizon_seconds < 0:
            raise ValueError("notional/leverage must be positive and horizon non-negative")
        common = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "notional_usd": float(notional_usd),
            "leverage": float(leverage),
            "holding_horizon_seconds": float(holding_horizon_seconds),
        }
        fees = await _resolve(self.taker_fee_rate_provider, **common)
        funding = await _resolve(self.funding_rate_provider, **common)
        slippage = await _resolve(self.depth_slippage_bps_provider, **common)
        liquidation = await _resolve(self.liquidation_distance_provider, **common)

        entry_fee_rate = float(fees["entry_taker_rate"])
        exit_fee_rate = float(fees["exit_taker_rate"])
        # Fee rates are decimal fractions of notional, converted to bps.
        round_trip_fee_bps = (entry_fee_rate + exit_fee_rate) * 10000.0
        # Provider returns signed funding rate per funding interval and the
        # interval duration in seconds; credits remain negative costs.
        funding_rate = float(funding["rate"])
        interval_seconds = float(funding.get("interval_seconds", 8 * 3600))
        if interval_seconds <= 0:
            raise ValueError("funding interval must be positive")
        intervals = float(holding_horizon_seconds) / interval_seconds
        direction = 1.0 if side.upper() == "LONG" else -1.0
        funding_cost_bps = funding_rate * direction * intervals * 10000.0
        slippage_bps = float(slippage["round_trip_bps"])
        liquidation_distance_pct = float(liquidation["distance_pct"])
        return ExecutionCostQuote(
            venue=self.venue,
            symbol=symbol.upper(),
            side=side.upper(),
            round_trip_fee_bps=round_trip_fee_bps,
            expected_slippage_bps=slippage_bps,
            funding_cost_bps=funding_cost_bps,
            liquidation_distance_pct=liquidation_distance_pct,
            observed_at=datetime.now(timezone.utc).isoformat(),
            valid_for_seconds=self.valid_for_seconds,
            source=self.source,
        )
