from datetime import datetime, timezone
import json

import pytest

from aitos.capital_intelligence.explorer import CapitalEntityExplorer


class FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeClickHouse:
    def __init__(self):
        self.calls = []

    async def query(self, query, parameters=None):
        self.calls.append((query, parameters))
        if "GROUP BY venue, address" in query:
            return FakeResult(
                [
                    (
                        "1",
                        "0x" + "a" * 40,
                        3,
                        2500.0,
                        2000.0,
                        500.0,
                        2,
                        4,
                        datetime(2026, 1, 1, tzinfo=timezone.utc),
                    )
                ]
            )
        if "SELECT payload_json" in query:
            return FakeResult(
                [
                    (
                        json.dumps(
                            {
                                "event_id": "evt-1",
                                "finality": "confirmed",
                                "actor_id": "0x" + "b" * 40,
                                "counterparty_id": "0x" + "a" * 40,
                                "metadata": {
                                    "actor_identity_kind": "evm_address",
                                    "flow_sign": -1,
                                },
                            }
                        ),
                    )
                ]
            )
        if "SELECT subject_id" in query:
            return FakeResult(
                [
                    (
                        "0x" + "a" * 40,
                        "0x" + "b" * 40,
                        "shared_funder_candidate",
                        "shared_funding_pattern",
                        "tx:1",
                        datetime(2026, 1, 1, tzinfo=timezone.utc),
                        0.4,
                        "candidate",
                    )
                ]
            )
        return FakeResult(
            [
                (
                    "exchange hot wallet",
                    "wallet_type",
                    "operator",
                    1,
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    None,
                )
            ]
        )


@pytest.mark.asyncio
async def test_entity_explorer_lists_chain_scoped_unverified_addresses():
    client = FakeClickHouse()
    explorer = CapitalEntityExplorer(client)
    entities = await explorer.list_entities(limit=20, offset=5)

    assert entities[0]["entity_id"] == "1:" + "0x" + "a" * 40
    assert entities[0]["gross_flow_usd"] == 2500.0
    assert entities[0]["identity_status"] == "unverified_address"
    assert client.calls[0][1] == {"limit": 20, "offset": 5}
    assert "finality != 'reverted'" in client.calls[0][0]


@pytest.mark.asyncio
async def test_entity_explorer_returns_events_evidence_and_temporal_labels():
    client = FakeClickHouse()
    explorer = CapitalEntityExplorer(client)
    entity = await explorer.get_entity("1", "0x" + "a" * 40)

    assert entity["beneficial_owner_verified"] is False
    assert entity["events"][0]["event_id"] == "evt-1"
    assert entity["events"][0]["entity_role"] == "counterparty"
    assert entity["events"][0]["entity_flow_sign"] == 1
    detail_query = client.calls[0][0]
    assert "actor_is_pool_caller_not_verified_beneficial_owner" in detail_query
    assert "'aggregate_only'" in detail_query
    assert "0x000000000000000000000000000000000000dead" in detail_query
    assert entity["evidence"][0]["status"] == "candidate"
    assert entity["labels"][0]["verified"] is True
    assert client.calls[1][1] == {"entity_id": "1:" + "0x" + "a" * 40}
    assert client.calls[2][1] == {"entity_id": "1:" + "0x" + "a" * 40}
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_entity_explorer_validates_bounds_and_identifiers():
    explorer = CapitalEntityExplorer(FakeClickHouse())
    with pytest.raises(ValueError, match="limit"):
        await explorer.list_entities(limit=101)
    with pytest.raises(ValueError, match="chain"):
        await explorer.get_entity("bad chain", "wallet")
    with pytest.raises(ValueError, match="event_limit"):
        await explorer.get_entity("1", "wallet", event_limit=501)
