from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aitos.capital_intelligence.contracts import CapitalEvent
from aitos.world_intelligence.capital_flow import CapitalFlowIntelligenceAdapter


class FakeClickHouse:
    def __init__(self, rows):
        self.rows = rows
        self.query_text = None
        self.parameters = None

    async def query(self, query, parameters):
        self.query_text = query
        self.parameters = parameters
        return SimpleNamespace(result_rows=self.rows)


def make_event(event_id, *, finality="confirmed", flow_sign=-1):
    now = datetime.now(timezone.utc)
    return CapitalEvent(
        event_id,
        "evm:1",
        "onchain_transfer",
        "crypto_spot",
        "1",
        "BTC",
        "erc20_transfer",
        now - timedelta(seconds=10),
        now,
        1,
        "BTC",
        100_000,
        actor_id="0xwallet",
        finality=finality,
        confidence=0.9,
        metadata={"flow_sign": flow_sign},
    )


@pytest.mark.asyncio
async def test_capital_flow_bridge_preserves_provenance_without_inventing_sentiment():
    event = make_event("event-1")
    client = FakeClickHouse([(event.to_json(), event.observed_time, event.event_id)])
    adapter = CapitalFlowIntelligenceAdapter(
        clickhouse=client,
        initial_lookback_hours=6,
        max_events=20,
    )
    events, cursor = await adapter.poll()
    assert len(events) == 1
    item = events[0]
    assert item.sentiment == 0.0
    assert item.metadata["sentiment_not_inferred_from_transfer"] is True
    assert item.metadata["strategy_use_approved"] is False
    assert item.metadata["actor_id"] == "0xwallet"
    assert item.source.is_primary is True
    assert "capital_flow_events FINAL" in client.query_text
    assert client.parameters["limit"] == 20
    assert client.parameters["observed_after"] < event.observed_time.isoformat()
    assert "event_id" in cursor


@pytest.mark.asyncio
async def test_capital_flow_bridge_skips_reverted_events_but_advances_cursor():
    reverted = make_event("event-1", finality="reverted")
    confirmed = make_event("event-2")
    client = FakeClickHouse(
        [
            (reverted.to_json(), reverted.observed_time, reverted.event_id),
            (confirmed.to_json(), confirmed.observed_time, confirmed.event_id),
        ]
    )
    adapter = CapitalFlowIntelligenceAdapter(clickhouse=client)
    events, cursor = await adapter.poll()
    assert [event.event_id for event in events] == ["event-2"]
    assert '"event_id":"event-2"' in cursor


def test_capital_flow_bridge_rejects_naive_or_invalid_cursors():
    adapter = CapitalFlowIntelligenceAdapter(clickhouse=FakeClickHouse([]))
    with pytest.raises(ValueError):
        adapter._parse_cursor('{"observed_time":"2026-01-01T00:00:00","event_id":"x"}')
    with pytest.raises(ValueError):
        adapter._parse_cursor("not-json")
