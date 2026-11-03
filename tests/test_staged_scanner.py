from types import SimpleNamespace

import pytest

from aitos.intelligence import staged_scanner


class FakeExchange:
    async def fetch_klines(self, symbol, timeframe, limit=100):
        rank = int(symbol.removeprefix("S"))
        return [
            SimpleNamespace(volume=float(rank), close=100.0 + rank) for _ in range(40)
        ]


class FakeCandidate:
    def __init__(self, symbol, score):
        self.symbol = symbol
        self.composite_score = score


class FakeScanner:
    def __init__(self):
        self._symbols = [f"S{i}" for i in range(1, 61)]
        self._reference_symbol = "S1"
        self._timeframe = "15m"
        self._kline_lookback = 40
        self._exchange = FakeExchange()
        self._last_scan_at = None
        self._last_candidate_count = 0
        self.scan_calls = []

    async def scan_symbol(self, symbol, reference_klines):
        self.scan_calls.append(symbol)
        return FakeCandidate(symbol, float(symbol.removeprefix("S")))


@pytest.mark.asyncio
async def test_staged_scan_calls_expensive_path_only_for_top_ten(monkeypatch):
    monkeypatch.setattr(staged_scanner.indicators, "momentum_score", lambda _: 10.0)
    monkeypatch.setattr(staged_scanner.indicators, "atr_percentile", lambda _: 60.0)
    monkeypatch.setattr(staged_scanner.indicators, "adx", lambda _: 100.0)
    monkeypatch.setattr(
        staged_scanner.indicators, "classify_regime", lambda _: "trending"
    )

    scanner = FakeScanner()
    stages = []

    async def callback(symbols, stage):
        stages.append((stage, list(symbols)))

    await staged_scanner.install_staged_scan(scanner, callback)
    result = await scanner.scan_all()

    assert [stage for stage, _ in stages] == [
        "TOP_50",
        "TOP_25",
        "TOP_10",
        "TOP_5",
        "TOP_2",
    ]
    assert len(stages[0][1]) == 50
    assert len(stages[1][1]) == 25
    assert len(stages[2][1]) == 10
    assert len(stages[3][1]) == 5
    assert len(stages[4][1]) == 2
    assert len(scanner.scan_calls) == 10
    assert len(result) == 2
