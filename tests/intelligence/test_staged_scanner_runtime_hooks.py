from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aitos.intelligence.staged_scanner import install_staged_scan


class _Scanner:
    def __init__(self) -> None:
        self._staged_scan_installed = False
        self._symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        self._exchange = object()
        self._timeframe = "15m"
        self._kline_lookback = 80
        self._reference_symbol = "BTCUSDT"

    async def _lightweight_rank_symbols(self, *, limit: int) -> list[str]:
        return self._symbols[:limit]

    async def iter_candidate_batches(self):
        selected = await self._lightweight_rank_symbols(limit=10)
        yield [
            SimpleNamespace(
                symbol=symbol,
                composite_score=8.0 if symbol == "ETHUSDT" else 7.0,
            )
            for symbol in selected
        ]


def test_staged_subscriptions_follow_the_iterator_used_by_paper_runtime() -> None:
    async def run() -> None:
        scanner = _Scanner()
        notifications: list[tuple[list[str], str]] = []

        async def on_subscription_change(symbols: list[str], stage: str) -> None:
            notifications.append((list(symbols), stage))

        await install_staged_scan(scanner, on_subscription_change)

        batches = [batch async for batch in scanner.iter_candidate_batches()]
        assert len(batches) == 1
        assert [candidate.symbol for batch in batches for candidate in batch] == [
            "BTCUSDT",
            "ETHUSDT",
        ]
        assert notifications[:5] == [
            (["BTCUSDT", "ETHUSDT", "SOLUSDT"], "TOP_50"),
            (["BTCUSDT", "ETHUSDT", "SOLUSDT"], "TOP_25"),
            (["BTCUSDT", "ETHUSDT", "SOLUSDT"], "TOP_10"),
            (["BTCUSDT", "ETHUSDT", "SOLUSDT"], "TOP_5"),
            (["BTCUSDT", "ETHUSDT"], "TOP_2"),
        ]
        assert notifications[5:] == [(["ETHUSDT", "BTCUSDT"], "TOP_2")]

    asyncio.run(run())


def test_staged_subscription_hook_installation_is_idempotent() -> None:
    async def run() -> None:
        scanner = _Scanner()
        calls: list[str] = []

        async def on_subscription_change(symbols: list[str], stage: str) -> None:
            calls.append(stage)

        await install_staged_scan(scanner, on_subscription_change)
        await install_staged_scan(scanner, on_subscription_change)
        selected = await scanner._lightweight_rank_symbols(limit=10)

        assert selected == ["BTCUSDT", "ETHUSDT"]
        assert calls == ["TOP_50", "TOP_25", "TOP_10", "TOP_5", "TOP_2"]

    asyncio.run(run())
