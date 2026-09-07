"""Staged scanner orchestration for a bounded market-data workload."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from aitos.intelligence import indicators
from aitos.logging_setup import get_logger

logger = get_logger("aitos.intelligence.staged_scanner")

TOP_50 = 50
TOP_25 = 25
TOP_10 = 10
TOP_5 = 5
TOP_2 = 2
CHEAP_KLINE_CONCURRENCY = 6

SubscriptionCallback = Callable[[list[str], str], Awaitable[None]]


def _cheap_score(klines: list[Any]) -> float:
    """Candle-only score; deliberately avoids order-flow/book REST calls."""
    if len(klines) < 20:
        return -1.0
    try:
        momentum = float(indicators.momentum_score(klines))
        atr_percentile = float(indicators.atr_percentile(klines))
        adx = min(10.0, float(indicators.adx(klines)) / 10.0)
        regime = indicators.classify_regime(klines)
        regime_bonus = {
            "trending": 2.0,
            "expansion": 1.5,
            "compression": 0.75,
            "ranging": 0.5,
            "volatile": 0.25,
        }.get(regime, 0.0)
        volatility_fit = max(0.0, 10.0 - abs(atr_percentile - 60.0) / 6.0)
        return round(
            0.40 * momentum + 0.25 * volatility_fit + 0.25 * adx + 0.10 * regime_bonus,
            6,
        )
    except Exception:
        return -1.0


def _medium_score(klines: list[Any], cheap_score: float) -> float:
    """Add candle-volume quality without order-flow/book REST calls."""
    if cheap_score < 0 or len(klines) < 20:
        return -1.0
    try:
        recent = klines[-20:]
        volumes = [max(0.0, float(getattr(k, "volume", 0.0))) for k in recent]
        average = sum(volumes) / max(1, len(volumes))
        ratio = min(3.0, volumes[-1] / average) if average else 0.0
        volume_score = min(10.0, ratio * 4.0)
        return round(0.75 * cheap_score + 0.25 * volume_score, 6)
    except Exception:
        return cheap_score


async def install_staged_scan(
    scanner: Any,
    on_subscription_change: SubscriptionCallback,
) -> None:
    """Install the ALL→50→25→10→5→2 scanner pipeline.

    BTC is always retained as the deep anchor and is analyzed with the
    expensive scanner even when it would not rank inside the non-BTC cohort.
    Returned trading candidates are the final Top-2 non-BTC symbols.
    """
    if getattr(scanner, "_staged_scan_installed", False):
        return

    async def _fetch_klines(symbol: str) -> tuple[str, list[Any]]:
        try:
            return symbol, await scanner._exchange.fetch_klines(
                symbol, scanner._timeframe, limit=scanner._kline_lookback
            )
        except Exception as exc:
            logger.warning(
                "staged kline fetch failed",
                extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
            )
            return symbol, []

    async def _staged_scan_all() -> list[Any]:
        symbols = list(dict.fromkeys(scanner._symbols))
        anchor = scanner._reference_symbol or "BTCUSDT"
        reference_klines = None
        if anchor:
            try:
                reference_klines = await scanner._exchange.fetch_klines(
                    anchor, scanner._timeframe, limit=scanner._kline_lookback
                )
            except Exception as exc:
                logger.warning(
                    "reference kline fetch failed",
                    extra={"aitos_extra": {"error": str(exc)}},
                )

        semaphore = asyncio.Semaphore(CHEAP_KLINE_CONCURRENCY)

        async def fetch_limited(symbol: str) -> tuple[str, list[Any]]:
            async with semaphore:
                return await _fetch_klines(symbol)

        fetched = await asyncio.gather(*(fetch_limited(s) for s in symbols))
        kline_map = dict(fetched)
        cheap_scores = {symbol: _cheap_score(klines) for symbol, klines in fetched}

        top50 = [
            symbol
            for symbol, score in sorted(
                cheap_scores.items(), key=lambda item: item[1], reverse=True
            )
            if score >= 0
        ][:TOP_50]
        if anchor in symbols and anchor not in top50:
            top50 = [anchor, *top50[: TOP_50 - 1]]
        await on_subscription_change(top50, "TOP_50")

        medium_scores = {
            symbol: _medium_score(
                kline_map.get(symbol, []), cheap_scores.get(symbol, -1.0)
            )
            for symbol in top50
        }
        top25 = [
            symbol
            for symbol, score in sorted(
                medium_scores.items(), key=lambda item: item[1], reverse=True
            )
            if score >= 0
        ][:TOP_25]
        if anchor in top50 and anchor not in top25:
            top25 = [anchor, *top25[: TOP_25 - 1]]
        await on_subscription_change(top25, "TOP_25")

        # Expensive multi-source analysis is capped at nine ranked non-BTC
        # symbols plus the permanent BTC anchor.
        non_anchor = [symbol for symbol in top25 if symbol != anchor]
        expensive_symbols = ([anchor] if anchor in top25 else []) + non_anchor[
            : TOP_10 - 1
        ]
        expensive: list[Any] = []
        for symbol in expensive_symbols:
            try:
                candidate = await scanner.scan_symbol(symbol, reference_klines)
                if candidate is not None:
                    expensive.append(candidate)
            except Exception as exc:
                logger.error(
                    "staged expensive scan failed",
                    extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
                )

        expensive.sort(key=lambda candidate: candidate.composite_score, reverse=True)
        anchor_candidates = [c for c in expensive if c.symbol == anchor]
        non_anchor_candidates = [c for c in expensive if c.symbol != anchor]
        top10 = (
            anchor_candidates + non_anchor_candidates[: TOP_10 - len(anchor_candidates)]
        )
        await on_subscription_change([c.symbol for c in top10], "TOP_10")

        top5 = non_anchor_candidates[:TOP_5]
        await on_subscription_change([c.symbol for c in top5], "TOP_5")
        top2 = top5[:TOP_2]
        await on_subscription_change([c.symbol for c in top2], "TOP_2")

        scanner._last_scan_at = datetime.now(timezone.utc).isoformat()
        scanner._last_candidate_count = len(top2)
        logger.info(
            "staged scan complete",
            extra={
                "aitos_extra": {
                    "stage_counts": {
                        "ALL": len(symbols),
                        "TOP_50": len(top50),
                        "TOP_25": len(top25),
                        "TOP_10": len(top10),
                        "TOP_5": len(top5),
                        "TOP_2": len(top2),
                    },
                    "anchor": anchor,
                    "top2": [c.symbol for c in top2],
                }
            },
        )
        return top2

    scanner.scan_all = _staged_scan_all
    scanner._staged_scan_installed = True
