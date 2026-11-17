from datetime import datetime, timezone
from decimal import Decimal
import pytest

from aitos.capital_intelligence.contracts import CapitalEvent, EntityEvidence, EntityLabel
from aitos.capital_intelligence.ranking import rank_capital_candidates
from aitos.capital_intelligence.storage import ClickHouseCapitalStore
from aitos.capital_intelligence.token_registry import TokenMetadata, TokenRegistry


class FakeClickHouse:
    def __init__(self):
        self.commands = []
        self.inserts = []

    async def command(self, sql):
        self.commands.append(sql)

    async def insert(self, table, rows, column_names):
        self.inserts.append((table, rows, column_names))


@pytest.mark.asyncio
async def test_entity_evidence_and_temporal_labels_are_persisted():
    client = FakeClickHouse()
    store = ClickHouseCapitalStore(client)
    await store.ensure_schema()
    now = datetime.now(timezone.utc)
    evidence = EntityEvidence(
        "wallet:a", "wallet:b", "funded_by", "onchain", "tx:abc", now, 0.8
    )
    label = EntityLabel("wallet:a", "fund", "entity", "operator", False, now)
    assert await store.append_entity_evidence([evidence]) == 1
    assert await store.append_entity_labels([label]) == 1
    assert len(client.commands) == 3
    assert client.inserts[0][0] == "aitos.capital_entity_evidence"
    assert client.inserts[1][0] == "aitos.capital_entity_labels"
    assert client.inserts[0][1][0][0].startswith("evi_")
    assert client.inserts[1][1][0][0].startswith("lbl_")


def event(event_id, actor, usd, sign, source="rpc-a", counterparty="wallet:z"):
    now = datetime.now(timezone.utc)
    return CapitalEvent(
        event_id, source, "onchain_transfer", "crypto_spot", "1", "USDC",
        "erc20_transfer", now, now, 100, "USDC", usd,
        actor_id=actor, counterparty_id=counterparty,
        confidence=0.9, metadata={"flow_sign": sign},
    )


def test_ranking_is_explainable_and_excludes_aggregate_cohorts():
    rows = [
        event("1", "wallet:a", 100_000, 1),
        event("2", "wallet:a", 200_000, -1, "rpc-b", "wallet:y"),
        event("3", "wallet:a", 300_000, 1, "rpc-c", "wallet:x"),
        event("4", "wallet:b", 900_000, 1),
    ]
    aggregate = CapitalEvent(
        "agg", "binance", "exchange_aggregate", "crypto_futures", "binance",
        "BTCUSDT", "ratio", datetime.now(timezone.utc), datetime.now(timezone.utc),
        0, "ratio", None, metadata={"aggregate_only": True},
    )
    ranked = rank_capital_candidates(rows + [aggregate], market_liquidity_usd={"USDC": 1_000_000})
    assert [item.entity_id for item in ranked] == ["wallet:a"]
    assert ranked[0].event_count == 3
    assert ranked[0].source_count == 3
    assert ranked[0].component_scores["source_diversity"] == 1.0
    assert ranked[0].net_flow_usd == 200_000
    assert ranked[0].score < 100
    assert any("not a probability" in item for item in ranked[0].caveats)


def test_token_registry_preserves_decimal_precision():
    token = TokenMetadata("1", "0x" + "a" * 40, "asset:usdc", "USDC", 6)
    registry = TokenRegistry([token])
    assert registry.get("1", token.address).normalize_raw("123456789") == Decimal("123.456789")
    with pytest.raises(ValueError):
        token.normalize_raw("1.5")
