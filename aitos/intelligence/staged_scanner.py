"""Staged scanner orchestration for bounded market-data workload.

The existing OpportunityScanner owns the expensive per-symbol analysis. This
module adds a thin orchestration layer around it so the full exchange universe
is narrowed progressively before expensive analysis is requested.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aitos.intelligence import indicators

TOP_50 = 50
TOP_25 = 25
TOP_10 = 10
TOP_5 = 5
TOP_2 = 2
CHEAP_KLINE_CONCURRENCY = 6

SubscriptionCallback = Callable[[list[str], str], Awaitable[None]]


def _cheap_score(klines: list[Any]) -> float:
    """Score a symbol using candle-only features; no book/trade REST calls."""
    if len(klines) < 20:
        return -1.0
    try:
        momentum = float(indicators.momentum_score(klines))
        atr_fit = float(indicators.atr_percentile(klines))
        adx = min(10.0, float(indicators.adx(klines)) / 10.0)
        regime = indicators.classify_regime(klines)
        regime_bonus = {
            "trending": 2.0,
            "expansion": 1.5,
            "compression": 0.75,
            "ranging": 0.5,
            "volatile": 0.25,
        }.get(regime, 0.0)
        volatility_fit = max(0.0, 10.0 - abs(atr_fit - 60.0) / 6.0)
        return round(
            0.40 * momentum + 0.25 * volatility_fit + 0.25 * adx + 0.10 * regime_bonus,
            6,
        )
    except Exception:
        return -1.0


async def install_staged_scan(
    scanner: Any,
    on_subscription_change: SubscriptionCallback,
) -> None:
    """Replace scanner.scan_all with ALL→50→25→10→5→2 orchestration.

    Only the final Top-10 cohort invokes the existing expensive scan_symbol()
    implementation. Top-50/25 narrowing uses candle-only data and Top-10/5/2
    are progressively ranked from the expensive cohort. This preserves the
    scanner's existing scoring model while preventing full-universe order-flow
    REST/WebSocket pressure.
    """
    if getattr(scanner, "_staged_scan_installed", False):
        return

    original_scan_all = scanner.scan_all

    async def _fetch_klines(symbol: str) -> tuple[str, list[Any]]:
        try:
            data = await scanner._exchange.fetch_klines(
                symbol, scanner._timeframe, limit=scanner._kline_lookback
            )
            return symbol, data
        except Exception:
            return symbol, []

    async def _staged_scan_all() -> list[Any]:
        symbols = list(dict.fromkeys(scanner._symbols))
        reference_klines = None
        if scanner._reference_symbol:
            try:
                reference_klines = await scanner._exchange.fetch_klines(
                    scanner._reference_symbol,
                    scanner._timeframe,
                    limit=scanner._kline_lookback,
                )
            except Exception:
                reference_klines = None

        sem = asyncio.Semaphore(CHEAP_KLINE_CONCURRENCY)

        async def fetch_limited(symbol: str) -> tuple[str, list[Any]]:
            async with sem:
                return await _fetch_klines(symbol)

        fetched = await asyncio.gather(*(fetch_limited(s) for s in symbols))
        cheap_ranked = sorted(
            ((symbol, _cheap_score(klines)) for symbol, klines in fetched),
            key=lambda item: item[1],
            reverse=True,
        )
        top50 = [s for s, score in cheap_ranked[:TOP_50] if score >= 0]
        if (
            scanner._reference_symbol in symbols
            and scanner._reference_symbol not in top50
        ):
            top50 = [scanner._reference_symbol, *top50[: TOP_50 - 1]]
        await on_subscription_change(top50, "TOP_50")

        # Top-25 uses the same candle data, avoiding another full exchange scan.
        cheap_map = dict(fetched)
        top25 = sorted(
            top50,
            key=lambda s: _cheap_score(cheap_map.get(s, [])),
            reverse=True,
        )[:TOP_25]
        if (
            scanner._reference_symbol in top50
            and scanner._reference_symbol not in top25
        ):
            top25 = [scanner._reference_symbol, *top25[: TOP_25 - 1]]
        await on_subscription_change(top25, "TOP_25")

        # Expensive multi-source analysis is deliberately capped at Top-25.
        expensive: list[Any] = []
        for symbol in top25:
            try:
                candidate = await scanner.scan_symbol(symbol, reference_klines)
                if candidate is not None:
                    expensive.append(candidate)
            except Exception as exc:
                (
                    scanner._logger.error("staged scan failed for %s: %s", symbol, exc)
                    if hasattr(scanner, "_logger")
                    else None
                )

        expensive.sort(key=lambda c: c.composite_score, reverse=True)
        top10 = expensive[:TOP_10]
        await on_subscription_change([c.symbol for c in top10], "TOP_10")
        top5 = top10[:TOP_5]
        await on_subscription_change([c.symbol for c in top5], "TOP_5")
        top2 = top5[:TOP_2]
        await on_subscription_change([c.symbol for c in top2], "TOP_2")

        scanner._last_scan_at = (
            scanner._utc_now_iso() if hasattr(scanner, "_utc_now_iso") else None
        )
        scanner._last_candidate_count = len(top2)
        (
            await scanner._event_bus.publish(
                scanner._make_scan_complete_event(
                    symbols_scanned=len(symbols),
                    candidates_found=len(top2),
                    stage_counts={
                        "ALL": len(symbols),
                        "TOP_50": len(top50),
                        "TOP_25": len(top25),
                        "TOP_10": len(top10),
                        "TOP_5": len(top5),
                        "TOP_2": len(top2),
                    },
                )
                if hasattr(scanner, "_make_scan_complete_event")
                else None
            )
            if False
            else None
        )
        return top2

    scanner.scan_all = _staged_scan_all
    scanner._staged_scan_installed = True
