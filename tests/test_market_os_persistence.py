import asyncio
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


@pytest.mark.asyncio
async def test_market_os_persistence_can_restart_and_reports_flush_task_health():
    class Subscription:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class FakeBus:
        def __init__(self):
            self.subscriptions = []

        async def subscribe(self, *args, **kwargs):
            subscription = Subscription()
            self.subscriptions.append(subscription)
            return subscription

    repository = FakeRepository()
    bus = FakeBus()
    service = MarketOSPersistence(bus, repository, flush_interval_seconds=60.0)

    await service.initialize({})
    first_batch = list(bus.subscriptions)
    first_health = await service.health_check()
    assert first_health.status.value == "healthy"
    assert first_health.details["flush_task_alive"] is True

    await service.shutdown()
    assert all(subscription.cancelled for subscription in first_batch)
    assert (await service.health_check()).status.value == "unhealthy"

    await service.initialize({})
    assert len(bus.subscriptions) > len(first_batch)
    second_health = await service.health_check()
    assert second_health.status.value == "healthy"
    assert second_health.details["flush_task_alive"] is True

    await service.shutdown()


@pytest.mark.asyncio
async def test_market_os_persistence_reports_failure_and_recovers_after_retry():
    class FlakyClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.fail_next_insert = True

        async def insert(self, table, rows, column_names):
            if self.fail_next_insert:
                self.fail_next_insert = False
                raise RuntimeError("temporary ClickHouse insert failure")
            await super().insert(table, rows, column_names)

    repository = FakeRepository()
    repository._client = FlakyClient()
    service = MarketOSPersistence(None, repository, batch_size=1)
    service._initialized = True
    service._flush_task = asyncio.create_task(asyncio.sleep(60.0))
    event = Event(
        topic="market.orderflow.BTCUSDT",
        payload={"trade_count": 1},
        source_module="test",
    )

    with pytest.raises(RuntimeError, match="temporary ClickHouse"):
        await service._handle_orderflow(event)

    failed = await service.health_check()
    assert failed.status.value == "degraded"
    assert failed.details["errors"] == 1
    assert failed.details["pending_rows"] == 1
    assert failed.details["pending_batches"] == 1
    assert failed.details["failed_tables"] == ["market_orderflow"]
    assert failed.details["last_error_type"] == "RuntimeError"
    assert failed.details["last_failed_table"] == "market_orderflow"
    assert failed.details["last_retry_outcome"] == "requeued"
    assert failed.details["retry_attempts"] == 1

    await service._flush_table("market_orderflow", None)

    recovered = await service.health_check()
    assert recovered.status.value == "healthy"
    assert recovered.details["errors"] == 1
    assert recovered.details["failed_tables"] == []
    assert recovered.details["pending_rows"] == 0
    assert recovered.details["pending_batches"] == 0
    assert recovered.details["last_retry_outcome"] == "recovered"
    assert recovered.details["table_write_latency"]["market_orderflow"]["count"] == 2

    service._flush_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await service._flush_task
    service._flush_task = None
