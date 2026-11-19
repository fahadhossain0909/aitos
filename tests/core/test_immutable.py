import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from aitos.core.immutable import FrozenDict, FrozenList, freeze_value
from aitos.market_data.bus import market_event_from_wire, market_event_to_wire
from aitos.market_data.contracts import (
    MarketEvent,
    MarketEventType,
    MarketSource,
)
from aitos.markets.contracts import (
    AssetClass,
    Instrument,
    MarketEvent as DomainMarketEvent,
    MarketEventType as DomainMarketEventType,
)
from aitos.markets.state import GlobalMarketState


def test_frozen_containers_preserve_json_shape_and_reject_nested_mutation():
    value = freeze_value(
        {"levels": [{"price": 100.0, "sizes": [1, 2]}], "tags": ["spot"]}
    )

    assert isinstance(value, FrozenDict)
    assert isinstance(value["levels"], FrozenList)
    assert isinstance(value["levels"][0], FrozenDict)
    assert isinstance(value["levels"][0]["sizes"], FrozenList)
    assert value["levels"] == [{"price": 100.0, "sizes": [1, 2]}]
    assert json.loads(json.dumps(value)) == {
        "levels": [{"price": 100.0, "sizes": [1, 2]}],
        "tags": ["spot"],
    }

    with pytest.raises(TypeError, match="immutable"):
        value["new"] = True
    with pytest.raises(TypeError, match="immutable"):
        value["levels"][0]["price"] = 101.0
    with pytest.raises(TypeError, match="immutable"):
        value["levels"][0]["sizes"].append(3)


def test_frozen_containers_support_dataclass_asdict():
    state = GlobalMarketState(features={"signals": {"trend": 0.8}})
    result = asdict(state)
    assert result["features"]["signals"]["trend"] == 0.8
    with pytest.raises(TypeError, match="immutable"):
        result["features"]["signals"]["trend"] = 0.9


def test_market_data_event_payload_and_stage_timestamps_are_deeply_immutable():
    event = MarketEvent(
        event_type=MarketEventType.TICKER,
        exchange="binance",
        market="spot",
        symbol="BTCUSDT",
        event_time=datetime.now(timezone.utc),
        ingest_time=datetime.now(timezone.utc),
        payload={"book": [{"price": 100.0}]},
        source=MarketSource.WEBSOCKET,
        stage_timestamps={"decode": 1.0},
    )

    with pytest.raises(TypeError, match="immutable"):
        event.payload["book"][0]["price"] = 99.0
    with pytest.raises(TypeError, match="immutable"):
        event.stage_timestamps["decode"] = 2.0
    assert json.loads(json.dumps(event.payload)) == {"book": [{"price": 100.0}]}


def test_market_domain_event_payload_is_deeply_immutable():
    now = datetime.now(timezone.utc)
    event = DomainMarketEvent(
        event_type=DomainMarketEventType.QUOTE,
        instrument=Instrument(
            "BTCUSDT", AssetClass.CRYPTO, "binance", market_type="spot"
        ),
        event_time=now,
        payload={"levels": [{"price": 100.0}]},
        source="websocket",
        received_at=now,
        stage_timestamps={"normalize": 1.0},
    )
    with pytest.raises(TypeError, match="immutable"):
        event.payload["levels"][0]["price"] = 99.0

def test_immutable_canonical_event_round_trips_through_existing_wire_format():
    event = MarketEvent(
        event_type=MarketEventType.TICKER,
        exchange="binance",
        market="spot",
        symbol="BTCUSDT",
        event_time=datetime.now(timezone.utc),
        ingest_time=datetime.now(timezone.utc),
        payload={"book": [{"price": 100.0, "sizes": [1, 2]}]},
        source=MarketSource.WEBSOCKET,
        stage_timestamps={"decode": 1.0},
    )
    wire = json.loads(json.dumps(market_event_to_wire(event)))
    restored = market_event_from_wire(wire)

    assert restored.event_id == event.event_id
    assert restored.trace_id == event.trace_id
    assert restored.payload == event.payload
    assert restored.stage_timestamps == event.stage_timestamps
    with pytest.raises(TypeError, match="immutable"):
        restored.payload["book"][0]["sizes"].append(3)
