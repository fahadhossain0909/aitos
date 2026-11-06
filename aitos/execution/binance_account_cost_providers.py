"""Authenticated Binance USD-M cost-data providers.

This module wires the existing quote adapter to Binance account/market endpoints.
Liquidation distance remains an injected account-risk calculation because it
must model the account's margin mode, wallet balance, maintenance bracket and
the *proposed* position; using a current unrelated position would be unsafe.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from aitos.execution.binance_cost_adapter import BinanceUsdMExecutionCostAdapter


class BinanceUsdMAccountCostProviders:
    """Fetch commission, funding and depth impact from Binance USD-M APIs."""

    def __init__(
        self,
        executor: Any,
        *,
        liquidation_distance_provider: Callable[..., Any],
        depth_limit: int = 1000,
    ) -> None:
        if executor is None or not callable(liquidation_distance_provider):
            raise ValueError(
                "authenticated executor and liquidation-distance provider are required"
            )
        if depth_limit not in {100, 500, 1000}:
            raise ValueError("depth_limit must be one of 100, 500, or 1000")
        self.executor = executor
        self.liquidation_distance_provider = liquidation_distance_provider
        self.depth_limit = depth_limit

    async def _request(self, path: str, params: dict[str, Any]) -> Any:
        request = getattr(self.executor, "_signed_request", None)
        if not callable(request):
            raise RuntimeError("executor does not expose Binance signed API transport")
        result = request("GET", path, params)
        return await result if hasattr(result, "__await__") else result

    async def taker_fees(self, *, symbol: str, **_: Any) -> dict[str, float]:
        data = await self._request(
            "/fapi/v1/commissionRate", {"symbol": symbol.upper()}
        )
        rate = float(data["takerCommissionRate"])
        if not math.isfinite(rate) or rate < 0:
            raise ValueError("Binance returned an invalid taker commission rate")
        return {"entry_taker_rate": rate, "exit_taker_rate": rate}

    async def funding(self, *, symbol: str, **_: Any) -> dict[str, float]:
        premium = await self._request(
            "/fapi/v1/premiumIndex", {"symbol": symbol.upper()}
        )
        rate = float(premium["lastFundingRate"])
        # Binance USD-M's standard interval is eight hours. Symbols with a
        # configured non-standard interval are returned by fundingInfo.
        interval_seconds = 8.0 * 3600.0
        funding_info = await self._request("/fapi/v1/fundingInfo", {})
        for item in funding_info or []:
            if str(item.get("symbol", "")).upper() == symbol.upper():
                interval_hours = float(item["fundingIntervalHours"])
                if not math.isfinite(interval_hours) or interval_hours <= 0:
                    raise ValueError("Binance returned an invalid funding interval")
                interval_seconds = interval_hours * 3600.0
                break
        if not math.isfinite(rate):
            raise ValueError("Binance returned an invalid funding rate")
        return {"rate": rate, "interval_seconds": interval_seconds}

    @staticmethod
    def _sweep_vwap(levels: list[list[str]], quote_notional: float) -> float:
        remaining = quote_notional
        base_filled = 0.0
        for raw_price, raw_quantity in levels:
            price = float(raw_price)
            quantity = float(raw_quantity)
            if (
                not math.isfinite(price)
                or not math.isfinite(quantity)
                or price <= 0
                or quantity <= 0
            ):
                continue
            level_quote = price * quantity
            take_quote = min(remaining, level_quote)
            base_filled += take_quote / price
            remaining -= take_quote
            if remaining <= max(1e-8, quote_notional * 1e-10):
                break
        if remaining > max(1e-8, quote_notional * 1e-10) or base_filled <= 0:
            raise RuntimeError(
                "Binance order book depth is insufficient for requested notional"
            )
        return quote_notional / base_filled

    async def depth_slippage(
        self, *, symbol: str, side: str, notional_usd: float, **_: Any
    ) -> dict[str, float]:
        book = await self._request(
            "/fapi/v1/depth",
            {"symbol": symbol.upper(), "limit": self.depth_limit},
        )
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            raise RuntimeError("Binance returned an empty order book")
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if best_bid <= 0 or best_ask <= best_bid:
            raise RuntimeError("Binance order book is crossed or invalid")
        mid = (best_bid + best_ask) / 2.0
        entry_levels, exit_levels = (
            (asks, bids) if side.upper() == "LONG" else (bids, asks)
        )
        entry_vwap = self._sweep_vwap(entry_levels, float(notional_usd))
        exit_vwap = self._sweep_vwap(exit_levels, float(notional_usd))
        entry_bps = (
            (entry_vwap - mid) / mid
            if side.upper() == "LONG"
            else (mid - entry_vwap) / mid
        ) * 10000.0
        exit_bps = (
            (mid - exit_vwap) / mid
            if side.upper() == "LONG"
            else (exit_vwap - mid) / mid
        ) * 10000.0
        return {"round_trip_bps": max(0.0, entry_bps) + max(0.0, exit_bps)}

    async def liquidation_distance(self, **kwargs: Any) -> Any:
        result = self.liquidation_distance_provider(**kwargs)
        return await result if hasattr(result, "__await__") else result

    def build_adapter(self) -> BinanceUsdMExecutionCostAdapter:
        return BinanceUsdMExecutionCostAdapter(
            taker_fee_rate_provider=self.taker_fees,
            funding_rate_provider=self.funding,
            depth_slippage_bps_provider=self.depth_slippage,
            liquidation_distance_provider=self.liquidation_distance,
        )
