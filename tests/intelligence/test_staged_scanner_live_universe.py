from types import SimpleNamespace
import pytest
from aitos.intelligence.staged_scanner import install_staged_scan


class LiveUniverse:
    async def snapshot(self):
        rows = {
            "UNCONFIGURED": {
                "last_price": 100.0,
                "change_pct_24h": 0.0,
                "quote_volume_24h": 1e12,
                "range_pct_24h": 1.0,
            }
        }
        rows.update(
            {
                f"S{i}": {
                    "last_price": 100.0 + i,
                    "change_pct_24h": float(i),
                    "quote_volume_24h": float(100 - i) * 1e8,
                    "range_pct_24h": 1.0,
                }
                for i in range(60)
            }
        )
        return rows

    @staticmethod
    def score(row):
        return row["quote_volume_24h"] / (1.0 + row["range_pct_24h"])


class NoHistory:
    def __init__(self):
        self.calls = []

    async def analyze(self, symbol, *args, **kwargs):
        self.calls.append(symbol)
        return None, {
            "available": False,
            "reason": "not_available",
            "evidence_only": True,
        }

    async def persist(self, *args, **kwargs):
        return None


class ExchangeMustNotFetch:
    def __init__(self):
        self.calls = []

    async def fetch_klines(self, symbol, *args, **kwargs):
        if symbol != "S0":
            raise AssertionError("only the BTC/reference anchor may be fetched")
        self.calls.append(symbol)
        return ["anchor-reference"]


class Cooldown:
    def should_skip(self, symbol, fingerprint):
        return symbol == "S1"


class Scanner:
    def __init__(self):
        self._staged_scan_installed = False
        self._symbols = [f"S{i}" for i in range(60)]
        self._reference_symbol = "S0"
        self._timeframe = "15m"
        self._kline_lookback = 80
        self._exchange = ExchangeMustNotFetch()
        self._aitos_universe_market_context = LiveUniverse()
        self._aitos_historical_intelligence = NoHistory()
        self.scan_calls = []

    async def scan_symbol(self, symbol, reference_klines):
        assert reference_klines == ["anchor-reference"]
        self.scan_calls.append(symbol)
        return SimpleNamespace(symbol=symbol, composite_score=float(symbol[1:]))


@pytest.mark.asyncio
async def test_live_universe_path_uses_tickers_then_deep_scans_top_ten_only():
    scanner = Scanner()
    stages = []

    async def changed(symbols, stage):
        stages.append((stage, list(symbols)))

    await install_staged_scan(scanner, changed)
    result = await scanner.scan_all()
    assert scanner._exchange.calls == ["S0"]
    assert len(scanner.scan_calls) == 10
    assert len(result) == 2
    assert [s for s, _ in stages] == ["TOP_50", "TOP_25", "TOP_10", "TOP_5", "TOP_2"]
    assert stages[0][1][0] == "S0"
    assert stages[-1][1] == ["S9", "S8"]


class BatchScanner(Scanner):
    async def _lightweight_rank_symbols(self, limit=None):
        raise AssertionError("legacy candle-based universe ranking must not run")

    async def iter_candidate_batches(self):
        candidates = [
            SimpleNamespace(
                symbol=f"S{i}",
                composite_score=float(i),
                candidate_id=f"candidate-{i}",
                direction=SimpleNamespace(value="LONG"),
            )
            for i in range(4)
        ]
        yield candidates


@pytest.mark.asyncio
async def test_runtime_batch_path_uses_live_rank_and_enriches_only_promoted_top_two():
    scanner = BatchScanner()
    stages = []

    async def changed(symbols, stage):
        stages.append((stage, list(symbols)))

    await install_staged_scan(scanner, changed)
    scanner._candidate_rotation = Cooldown()
    selected = await scanner._lightweight_rank_symbols(limit=10)
    batches = [batch async for batch in scanner.iter_candidate_batches()]

    assert selected[:2] == ["S0", "S2"]
    assert len(batches) == 1
    assert scanner._aitos_historical_intelligence.calls == ["S3", "S2"]
    by_symbol = {item.symbol: item for item in batches[0]}
    assert by_symbol["S3"]._aitos_historical_context["available"] is False
    assert by_symbol["S2"]._aitos_historical_context["available"] is False
    assert (
        by_symbol["S0"]._aitos_historical_context["reason"]
        == "not_promoted_for_deep_history"
    )
