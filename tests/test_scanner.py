from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, List

import pytest

from aitos.exchange.base import ExchangeAdapter
from aitos.intelligence.scanner import OpportunityScanner, determine_direction
from aitos.models.market import (FundingRate, Kline, OpenInterest,
                                 OrderBookSnapshot)
from aitos.models.trade import TradeSide
from aitos.risk.models import PortfolioState
from aitos.trading.lifecycle import TradeLifecycle
from tests.test_indicators import make_klines, make_trending_up_klines

NOW = datetime.now(timezone.utc)


class FakeScannerExchange(ExchangeAdapter):
    """Deterministic exchange double: BTCUSDT trends up strongly (clear long
    setup), ETHUSDT stays flat/choppy (no clear edge, should be skipped)."""

    def __init__(self):
        self.connected = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True

    async def fetch_klines(self, symbol, timeframe, limit=500) -> List[Kline]:
        if symbol == "ETHUSDT":
            return make_klines(
                [100.0 + (0.3 if i % 2 == 0 else -0.3) for i in range(40)],
                taker_buy_ratio=0.5,
            )
        return make_trending_up_klines(n=40, start=100.0, step=2.0)

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
        return
        yield  # pragma: no cover

    async def stream_trades(self, symbols) -> AsyncIterator:
        return
        yield  # pragma: no cover

    async def stream_order_book(
        self, symbols, levels=20
    ) -> AsyncIterator[OrderBookSnapshot]:
        return
        yield  # pragma: no cover


def test_determine_direction_bullish_bos_with_confirming_flow():
    assert determine_direction("bullish_bos", cvd_score=6.0) == TradeSide.LONG


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
    assert "regime=" in candidate.rationale[-1]


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
