from datetime import datetime, timezone

import pytest

from aitos.core.contracts import Event
from aitos.data.market_os_persistence import MarketOSPersistence


class FakeClient:
    def __init__(self):
        self.commands = []
        self.inserts = []

    async def command(self, sql):
        self.commands.append(sql)

    async def insert(self, table, rows, column_names):
        self.inserts.append((table, rows, column_names))


class FakeRepository:
    def __init__(self):
        self._client = FakeClient()


@pytest.mark.asyncio
async def test_persists_orderflow_event():
    repository = FakeRepository()
    service = MarketOSPersistence(None, repository, batch_size=1)
    event = Event(
        topic="market.orderflow.BTCUSDT",
        payload={
            "trade_count": 3,
            "buy_volume": 2.0,
            "sell_volume": 1.0,
            "delta": 1.0,
            "cvd": 5.0,
            "buy_ratio": 0.66,
            "aggression": 0.2,
            "imbalance": 0.1,
            "vwap": 100.0,
            "last_price": 101.0,
            "direction": "BUY",
        },
        source_module="test",
    )

    await service._handle_orderflow(event)

    assert repository._client.inserts[0][0] == "market_orderflow"
    assert repository._client.inserts[0][1][0][2] == "BTCUSDT"
    assert service._events_persisted == 1


@pytest.mark.asyncio
async def test_batches_market_os_events():
    repository = FakeRepository()
    service = MarketOSPersistence(None, repository, batch_size=2)

    for _ in range(2):
        await service._handle_orderflow(
            Event(
                topic="market.orderflow.BTCUSDT",
                payload={"trade_count": 1},
                source_module="test",
            )
        )

    assert len(repository._client.inserts) == 1
    assert len(repository._client.inserts[0][1]) == 2
    assert service._events_persisted == 2


@pytest.mark.asyncio
async def test_persists_liquidity_and_live_state_events():
    repository = FakeRepository()
    service = MarketOSPersistence(None, repository, batch_size=1)
    now = datetime.now(timezone.utc).isoformat()

    await service._handle_liquidity(
        Event(
            topic="market.liquidity.ETHUSDT",
            payload={
                "kind": "wall",
                "side": "bid",
                "score": 0.9,
                "price": 2000.0,
                "details": {"size": 10},
                "last_update_id": 123,
            },
            source_module="test",
        )
    )
    await service._handle_live_state(
        Event(
            topic="market.live_state.ETHUSDT",
            payload={
                "trade_count": 4,
                "order_flow": {"delta": 2},
                "liquidity_events": [],
                "best_bid": 1999.5,
                "best_ask": 2000.5,
                "timestamp": now,
            },
            source_module="test",
        )
    )

    assert [item[0] for item in repository._client.inserts] == [
        "market_liquidity_events",
        "market_live_state",
    ]
    assert service._events_persisted == 2


@pytest.mark.asyncio
async def test_persists_decision_risk_and_trade_analytics():
    repository = FakeRepository()
    service = MarketOSPersistence(None, repository, batch_size=1)

    events = [
        Event(
            topic="decision.snapshot",
            payload={
                "symbol": "BTCUSDT",
                "decision_id": "d-1",
                "confidence": 0.82,
                "signal": "LONG",
            },
            source_module="contextual-decision",
            correlation_id="corr-1",
        ),
        Event(
            topic="risk.score_update",
            payload={"symbol": "BTCUSDT", "risk_score": 0.18},
            source_module="risk-engine",
        ),
        Event(
            topic="trade.position_closed",
            payload={"symbol": "BTCUSDT", "trade_id": "t-1", "pnl": 42.0},
            source_module="trade-lifecycle",
        ),
        Event(
            topic="journey.snapshot",
            payload={"symbol": "BTCUSDT", "state": "EXTENDING", "health_score": 88.0},
            source_module="trade-journey",
        ),
    ]

    for event in events:
        await service._handle_live_analytics(event)

    assert len(repository._client.inserts) == 4
    assert all(
        item[0] == "live_analytics_events" for item in repository._client.inserts
    )
    rows = [item[1][0] for item in repository._client.inserts]
    assert [row[3] for row in rows] == [
        "decision.snapshot",
        "risk.score_update",
        "trade.position_closed",
        "journey.snapshot",
    ]
    assert rows[0][6] == "BTCUSDT"
    assert rows[0][8] == "corr-1"


@pytest.mark.asyncio
async def test_live_analytics_subscriptions_are_live_only():
    class FakeBus:
        def __init__(self):
            self.calls = []

        async def subscribe(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return object()

    repository = FakeRepository()
    bus = FakeBus()
    service = MarketOSPersistence(bus, repository)
    await service.initialize({})

    analytics_calls = [
        kwargs
        for args, kwargs in bus.calls
        if args and args[0] in service.LIVE_ANALYTICS_TOPICS
    ]
    assert len(analytics_calls) == len(service.LIVE_ANALYTICS_TOPICS)
    assert all(call["live_only"] is True for call in analytics_calls)
