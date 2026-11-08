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
        yield [
            SimpleNamespace(symbol="ETHUSDT", composite_score=8.0),
            SimpleNamespace(symbol="BTCUSDT", composite_score=7.0),
        ]
        yield [SimpleNamespace(symbol="SOLUSDT", composite_score=9.0)]


def test_staged_subscriptions_follow_the_iterator_used_by_paper_runtime() -> None:
    async def run() -> None:
        scanner = _Scanner()
        notifications: list[tuple[list[str], str]] = []

        async def on_subscription_change(symbols: list[str], stage: str) -> None:
            notifications.append((list(symbols), stage))

        await install_staged_scan(scanner, on_subscription_change)

        selected = await scanner._lightweight_rank_symbols(limit=10)
        assert selected == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        assert notifications == [(["BTCUSDT", "ETHUSDT", "SOLUSDT"], "TOP_50")]

        batches = [batch async for batch in scanner.iter_candidate_batches()]
        assert len(batches) == 2
        assert [
            candidate.symbol for batch in batches for candidate in batch
        ] == ["ETHUSDT", "BTCUSDT", "SOLUSDT"]
        assert notifications[1:] == [
            (["SOLUSDT", "ETHUSDT", "BTCUSDT"], "TOP_5"),
            (["SOLUSDT", "ETHUSDT"], "TOP_2"),
        ]

    asyncio.run(run())


def test_staged_subscription_hook_installation_is_idempotent() -> None:
    async def run() -> None:
        scanner = _Scanner()
        calls: list[str] = []

        async def on_subscription_change(symbols: list[str], stage: str) -> None:
            calls.append(stage)

        await install_staged_scan(scanner, on_subscription_change)
        await install_staged_scan(scanner, on_subscription_change)
        await scanner._lightweight_rank_symbols(limit=10)

        assert calls == ["TOP_50"]

    asyncio.run(run())
