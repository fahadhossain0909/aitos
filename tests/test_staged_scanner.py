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



@pytest.mark.asyncio
async def test_adaptive_scanner_capacity_preserves_anchor_and_open_position():
    from types import SimpleNamespace

    from aitos.compute.resource_governor import ResourceBudget
    from aitos.intelligence.scanner import OpportunityScanner

    class Rotation:
        def should_skip(self, symbol, fingerprint):
            return False

        def snapshot(self):
            return {}

    class Exchange:
        async def fetch_klines(self, symbol, timeframe, limit=20):
            return [
                SimpleNamespace(
                    close=100.0 + index,
                    volume=100.0 + index,
                    high=102.0 + index,
                    low=99.0 + index,
                    is_closed=True,
                )
                for index in range(20)
            ]

    scanner = SimpleNamespace(
        _symbols=[f"S{index}" for index in range(1, 7)],
        _reference_symbol="S1",
        _timeframe="1m",
        _exchange=Exchange(),
        _tuning=SimpleNamespace(
            analysis=SimpleNamespace(
                expensive_analysis_limit=5,
                lightweight_concurrency=3,
                timeout_seconds=1.0,
            )
        ),
        _lightweight_fingerprints={},
        _candidate_rotation=Rotation(),
        _aitos_adaptive_compute_runtime=SimpleNamespace(
            budget=ResourceBudget(
                cpu_workers=2,
                memory_budget_mb=512,
                background_task_limit=1,
                deep_analysis_slots=2,
                cpu_capacity_units=300.0,
            )
        ),
        _aitos_protected_symbols={"S2"},
    )

    from aitos.compute.telemetry_collector import SymbolTelemetryCollector

    scanner._aitos_symbol_telemetry_collector = SymbolTelemetryCollector()
    selected = await OpportunityScanner._lightweight_rank_symbols(scanner, limit=5)

    assert "S1" in selected
    assert "S2" in selected
    assert len(selected) == 3
