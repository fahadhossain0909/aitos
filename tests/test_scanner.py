import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from aitos.config.tuning import TuningSettings
from aitos.exchange.base import ExchangeAdapter
from aitos.intelligence.htf_cache import timeframe_delta
from aitos.intelligence.scanner import (
    OpportunityScanner,
    ScanCandidate,
    determine_direction,
    higher_timeframe_alignment,
    resolve_higher_timeframe,
)
from aitos.models.market import FundingRate, Kline, OpenInterest, OrderBookSnapshot
from aitos.models.trade import TradeSide
from aitos.risk.models import PortfolioState
from aitos.trading.lifecycle import TradeLifecycle
from tests.test_indicators import make_klines, make_trending_up_klines

NOW = datetime.now(timezone.utc)


class FakeScannerExchange(ExchangeAdapter):
    """Deterministic exchange double: BTCUSDT trends up strongly (clear long
    setup), ETHUSDT stays flat/choppy (no clear edge, should be skipped).

    Streaming methods intentionally remain alive after startup. The canonical
    market-data runtime treats an exhausted stream as a reconnect condition;
    returning immediately here therefore creates a tight reconnect loop during
    full-system wiring tests. Sleeping keeps the fake stream connected until
    the runtime cancels it during shutdown, matching a real long-lived socket.
    """

    def __init__(self):
        self.connected = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True

    async def fetch_klines(self, symbol, timeframe, limit=500) -> list[Kline]:
        if symbol == "ETHUSDT":
            candles = make_klines(
                [100.0 + (0.3 if i % 2 == 0 else -0.3) for i in range(40)],
                taker_buy_ratio=0.5,
            )
        else:
            candles = make_trending_up_klines(n=40, start=100.0, step=2.0)
        interval = timeframe_delta(timeframe)
        latest_close = datetime.now(timezone.utc) - interval / 2
        return [
            replace(
                candle,
                timeframe=timeframe,
                open_time=latest_close - interval * (len(candles) - index),
                close_time=latest_close - interval * (len(candles) - index - 1),
            )
            for index, candle in enumerate(candles)
        ]

    async def fetch_order_book(self, symbol, limit=50) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            symbol=symbol,
            bids=((99.9, 10.0),),
            asks=((100.0, 10.0),),
            last_update_id=1,
            timestamp=NOW,
        )

    async def fetch_recent_trades(self, symbol, limit=500):
        return []

    async def fetch_funding_rate(self, symbol) -> FundingRate:
        return FundingRate(
            symbol=symbol, funding_rate=-0.0003, funding_time=NOW, mark_price=100.0
        )

    async def fetch_open_interest(self, symbol) -> OpenInterest:
        return OpenInterest(symbol=symbol, open_interest=10_000.0, timestamp=NOW)

    async def stream_klines(self, symbols, timeframe) -> AsyncIterator[Kline]:
        await asyncio.sleep(3600)
        yield  # pragma: no cover

    async def stream_trades(self, symbols) -> AsyncIterator:
        await asyncio.sleep(3600)
        yield  # pragma: no cover

    async def stream_order_book(
        self, symbols, levels=20
    ) -> AsyncIterator[OrderBookSnapshot]:
        await asyncio.sleep(3600)
        yield  # pragma: no cover


def test_determine_direction_bullish_bos_with_confirming_flow():
    assert determine_direction("bullish_bos", cvd_score=7.0) == TradeSide.LONG


def test_determine_direction_bullish_bos_without_confirming_flow_is_none():
    assert determine_direction("bullish_bos", cvd_score=3.0) is None


def test_determine_direction_no_structure_relies_on_strong_cvd():
    assert determine_direction("none", cvd_score=7.0) == TradeSide.LONG
    assert determine_direction("none", cvd_score=3.0) == TradeSide.SHORT
    assert determine_direction("none", cvd_score=5.0) is None


@pytest.mark.asyncio
async def test_scan_symbol_passes_direction_and_component_scores_to_rl_scorer(
    event_bus,
):
    from aitos.intelligence.rl_policy import RLPolicyScorer

    captured_contexts = []

    class SpyRLScorer(RLPolicyScorer):
        async def score(self, symbol, context):
            captured_contexts.append(context)
            return 5.0

    exchange = FakeScannerExchange()
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=exchange,
        symbols=["BTCUSDT"],
        reference_symbol="",
        rl_scorer=SpyRLScorer(),
    )
    await scanner.initialize({})

    await scanner.scan_symbol("BTCUSDT")

    assert len(captured_contexts) == 1
    assert captured_contexts[0]["direction"] == "LONG"
    assert "trend_strength" in captured_contexts[0]
    assert "regime" in captured_contexts[0]


@pytest.mark.asyncio
async def test_scan_symbol_finds_long_setup_for_trending_symbol(event_bus):
    exchange = FakeScannerExchange()
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=exchange,
        symbols=["BTCUSDT", "ETHUSDT"],
        reference_symbol="",
    )
    await scanner.initialize({})

    candidate = await scanner.scan_symbol("BTCUSDT")

    assert candidate is not None
    assert candidate.direction == TradeSide.LONG
    assert 0 <= candidate.composite_score <= 100
    assert candidate.atr > 0
    assert any("regime=" in line for line in candidate.rationale)


@pytest.mark.asyncio
async def test_scan_symbol_returns_none_for_choppy_symbol(event_bus):
    exchange = FakeScannerExchange()
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=exchange,
        symbols=["BTCUSDT", "ETHUSDT"],
        reference_symbol="",
    )
    await scanner.initialize({})

    candidate = await scanner.scan_symbol("ETHUSDT")

    assert candidate is None


@pytest.mark.asyncio
async def test_scan_all_and_rank_returns_top_candidates(event_bus):
    exchange = FakeScannerExchange()
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=exchange,
        symbols=["BTCUSDT", "ETHUSDT"],
        reference_symbol="",
        min_score_threshold=0.0,
        top_n=5,
    )
    await scanner.initialize({})

    candidates = await scanner.scan_all()
    ranked = await scanner.rank(candidates)

    assert len(candidates) == 1
    assert ranked[0].symbol == "BTCUSDT"

    health = await scanner.health_check()
    assert health.details["last_candidate_count"] == 1

    await scanner.shutdown()
    assert exchange.closed is True


@pytest.mark.asyncio
async def test_rank_filters_below_threshold(event_bus):
    exchange = FakeScannerExchange()
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=exchange,
        symbols=["BTCUSDT"],
        reference_symbol="",
        min_score_threshold=999.0,
    )
    await scanner.initialize({})
    candidates = await scanner.scan_all()
    ranked = await scanner.rank(candidates)
    assert ranked == []


@pytest.mark.asyncio
async def test_to_opportunity_places_atr_based_sl_and_r_multiple_tps(event_bus):
    exchange = FakeScannerExchange()
    scanner = OpportunityScanner(
        event_bus=event_bus, exchange=exchange, symbols=["BTCUSDT"], reference_symbol=""
    )
    await scanner.initialize({})
    candidate = await scanner.scan_symbol("BTCUSDT")

    opportunity = scanner.to_opportunity(
        candidate, risk_reward_multiples=(1.0, 2.0, 3.0), atr_stop_multiplier=1.5
    )

    assert opportunity.side == TradeSide.LONG
    assert opportunity.stop_loss_price < opportunity.entry_price
    stop_distance = opportunity.entry_price - opportunity.stop_loss_price
    assert len(opportunity.take_profit_levels) == 3
    assert opportunity.take_profit_levels[0] == pytest.approx(
        opportunity.entry_price + stop_distance * 1.0
    )
    assert opportunity.take_profit_levels[2] == pytest.approx(
        opportunity.entry_price + stop_distance * 3.0
    )
    assert opportunity.trailing_sl_enabled is True
    assert 0.0 <= opportunity.confidence <= 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adx", "expected_last_r"),
    [(20.0, 3.0), (30.0, 2.5), (40.0, 3.3)],
)
async def test_default_final_target_adapts_to_confirmed_htf_adx(
    event_bus, adx, expected_last_r
):
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=FakeScannerExchange(),
        symbols=["BTCUSDT"],
        reference_symbol="",
    )
    candidate = ScanCandidate(
        symbol="BTCUSDT",
        direction=TradeSide.LONG,
        composite_score=80.0,
        component_scores={},
        component_availability={},
        rationale=[],
        entry_price=100.0,
        atr=1.0,
        regime="trending",
        htf_adx=adx,
    )
    opportunity = scanner.to_opportunity(candidate)
    stop_distance = candidate.atr * 1.5
    assert opportunity.take_profit_levels[-1] == pytest.approx(
        candidate.entry_price + stop_distance * expected_last_r
    )


@pytest.mark.asyncio
async def test_scanner_to_lifecycle_end_to_end(event_bus, risk_engine):
    exchange = FakeScannerExchange()
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=exchange,
        symbols=["BTCUSDT", "ETHUSDT"],
        reference_symbol="",
        min_score_threshold=0.0,
    )
    await scanner.initialize({})
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})

    candidates = await scanner.scan_all()
    ranked = await scanner.rank(candidates)
    assert ranked

    opportunity = scanner.to_opportunity(ranked[0])
    portfolio = PortfolioState(
        equity_usd=10_000.0, peak_equity_usd=10_000.0, volatility_percentile=30.0
    )

    trade = await lifecycle.submit_opportunity(opportunity, portfolio)

    assert trade.state.value == "position_opened"
    assert trade.symbol == "BTCUSDT"
    assert len(trade.take_profit_levels) == 3


@pytest.mark.asyncio
async def test_scanner_ignores_in_progress_rest_candle(event_bus):
    class FormingCandleExchange(FakeScannerExchange):
        async def fetch_klines(self, symbol, timeframe, limit=500):
            candles = await super().fetch_klines(symbol, timeframe, limit)
            interval = timeframe_delta(timeframe)
            forming = replace(
                candles[-1],
                open_time=candles[-1].close_time,
                close_time=candles[-1].close_time + interval,
                is_closed=False,
            )
            return [*candles, forming]

    exchange = FormingCandleExchange()
    scanner = OpportunityScanner(
        event_bus=event_bus, exchange=exchange, symbols=["BTCUSDT"], reference_symbol=""
    )
    await scanner.initialize({})
    expected = (await exchange.fetch_klines("BTCUSDT", "15m", 40))[-2].close
    candidate = await scanner.scan_symbol("BTCUSDT")
    assert candidate is not None
    assert candidate.entry_price == expected
    assert "entry_timing" in candidate.component_scores
    assert "breakout_acceptance" in candidate.component_scores


@pytest.mark.asyncio
async def test_live_scanner_blocks_when_websocket_market_state_is_stale(event_bus):
    exchange = FakeScannerExchange()
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=exchange,
        symbols=["BTCUSDT"],
        reference_symbol="",
        tuning=TuningSettings(mode="live"),
    )
    await scanner.initialize({})
    assert await scanner.scan_symbol("BTCUSDT") is None
    await scanner.shutdown()


def test_resolve_higher_timeframe_uses_conservative_auto_mapping():
    assert resolve_higher_timeframe("15m") == "1h"
    assert resolve_higher_timeframe("5m") == "15m"
    assert resolve_higher_timeframe("15m", "4h") == "4h"


def test_htf_alignment_accepts_matching_directional_trend():
    aligned, details = higher_timeframe_alignment(
        make_trending_up_klines(n=80, step=2.0), TradeSide.LONG
    )
    assert aligned is True
    assert details["trend"] == "bullish"
    assert details["adx"] >= 25.0


def test_htf_alignment_rejects_countertrend_ltf_signal():
    aligned, details = higher_timeframe_alignment(
        make_trending_up_klines(n=80, step=2.0), TradeSide.SHORT
    )
    assert aligned is False
    assert details["reason"] == "ltf_signal_opposes_htf_trend"


def test_htf_alignment_fails_closed_for_choppy_or_insufficient_data():
    choppy = make_klines(
        [100.0 + (0.3 if i % 2 == 0 else -0.3) for i in range(80)],
        taker_buy_ratio=0.5,
    )
    aligned, details = higher_timeframe_alignment(choppy, TradeSide.LONG)
    assert aligned is False
    assert details["reason"] == "htf_trend_weak_or_choppy"

    aligned, details = higher_timeframe_alignment(
        make_trending_up_klines(n=20), TradeSide.LONG
    )
    assert aligned is False
    assert details["reason"] == "insufficient_htf_history"


@pytest.mark.asyncio
async def test_scanner_blocks_ltf_long_when_htf_is_bearish(event_bus):
    class CountertrendHTFExchange(FakeScannerExchange):
        async def fetch_klines(self, symbol, timeframe, limit=500):
            if timeframe == "1h":
                closes = [200.0 - i * 2.0 for i in range(80)]
                return make_klines(closes, taker_buy_ratio=0.35)
            return make_trending_up_klines(n=80, step=2.0)

    exchange = CountertrendHTFExchange()
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=exchange,
        symbols=["BTCUSDT"],
        reference_symbol="",
    )
    await scanner.initialize({})
    assert await scanner.scan_symbol("BTCUSDT") is None
    await scanner.shutdown()


@pytest.mark.asyncio
async def test_to_opportunity_preserves_statistical_forecast_identity(event_bus):
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=FakeScannerExchange(),
        symbols=["BTCUSDT"],
        reference_symbol="",
    )
    forecast = {
        "symbol": "BTCUSDT",
        "horizon": "15m",
        "side": "LONG",
        "model_id": "aitos-contract-statistical-stack",
        "model_version": "1",
        "as_of": "2026-11-07T10:00:00+00:00",
        "sample_size": 256,
        "forecast_volatility": 0.04,
        "reference_volatility": 0.02,
        "tail_probability": 0.2,
        "expected_shortfall": 0.05,
    }
    candidate = ScanCandidate(
        symbol="BTCUSDT",
        direction=TradeSide.LONG,
        composite_score=80.0,
        component_scores={"a_stat": 8.0},
        component_availability={"a_stat": True},
        rationale=["statistical forecast available"],
        entry_price=100.0,
        atr=1.0,
        regime="trending",
        statistical_forecast=forecast,
    )
    opportunity = scanner.to_opportunity(candidate)
    assert opportunity.timeframe == scanner._timeframe
    assert opportunity.agent_consensus["statistical_forecast"] == forecast
