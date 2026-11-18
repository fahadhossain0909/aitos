from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import json
import pytest

from aitos.capital_intelligence.contracts import (
    CapitalEvent,
    EntityEvidence,
    EntityLabel,
)
from aitos.capital_intelligence.ranking import rank_capital_candidates
from aitos.capital_intelligence.storage import ClickHouseCapitalStore
from aitos.capital_intelligence.token_registry import TokenMetadata, TokenRegistry


class FakeClickHouse:
    def __init__(self):
        self.commands = []
        self.inserts = []
        self.query_rows = []

    async def query(self, sql, parameters):
        return SimpleNamespace(result_rows=self.query_rows)

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
        event_id,
        source,
        "onchain_transfer",
        "crypto_spot",
        "1",
        "USDC",
        "erc20_transfer",
        now,
        now,
        100,
        "USDC",
        usd,
        actor_id=actor,
        counterparty_id=counterparty,
        confidence=0.9,
        metadata={"flow_sign": sign, "actor_identity_kind": "evm_address"},
    )


def test_ranking_is_explainable_and_excludes_aggregate_cohorts():
    rows = [
        event("1", "wallet:a", 100_000, 1),
        event("2", "wallet:a", 200_000, -1, "rpc-b", "wallet:y"),
        event("3", "wallet:a", 300_000, 1, "rpc-c", "wallet:x"),
        event("4", "wallet:b", 900_000, 1),
    ]
    aggregate = CapitalEvent(
        "agg",
        "binance",
        "exchange_aggregate",
        "crypto_futures",
        "binance",
        "BTCUSDT",
        "ratio",
        datetime.now(timezone.utc),
        datetime.now(timezone.utc),
        0,
        "ratio",
        None,
        metadata={"aggregate_only": True},
    )
    ranked = rank_capital_candidates(
        rows + [aggregate], market_liquidity_usd={"USDC": 1_000_000}
    )
    assert [item.entity_id for item in ranked] == ["1:wallet:a", "1:wallet:z"]
    assert ranked[0].event_count == 3
    assert ranked[0].source_count == 3
    assert ranked[0].component_scores["source_diversity"] == 1.0
    assert ranked[0].net_flow_usd == 200_000
    assert ranked[0].score < 100
    assert any("not a probability" in item for item in ranked[0].caveats)


def test_token_registry_preserves_decimal_precision():
    token = TokenMetadata("1", "0x" + "a" * 40, "asset:usdc", "USDC", 6)
    registry = TokenRegistry([token])
    assert registry.get("1", token.address).normalize_raw("123456789") == Decimal(
        "123.456789"
    )
    with pytest.raises(ValueError):
        token.normalize_raw("1.5")


@pytest.mark.asyncio
async def test_reorg_reconciliation_appends_reverted_event_version():
    client = FakeClickHouse()
    store = ClickHouseCapitalStore(client)
    now = datetime.now(timezone.utc)
    original = CapitalEvent(
        "evm-event",
        "evm:1",
        "onchain_transfer",
        "crypto_spot",
        "1",
        "0x" + "a" * 40,
        "erc20_transfer",
        now,
        now,
        100,
        "token_base_units",
        None,
        actor_id="0x" + "b" * 40,
        transaction_id="0x" + "c" * 64,
        block_number=5,
        block_hash="0xold",
        finality="confirmed",
        metadata={"raw_amount": "100"},
    )
    client.query_rows = [(original.to_json(), 5, "0xold")]
    count = await store.reconcile_evm_reorg(
        {
            "source_id": "evm:1",
            "from_block": 5,
            "to_block": 5,
            "canonical_hashes": {5: "0xnew"},
        }
    )
    assert count == 1
    table, rows, columns = client.inserts[-1]
    assert table == "aitos.capital_flow_events"
    assert rows[0][columns.index("finality")] == "reverted"
    payload = json.loads(rows[0][columns.index("payload_json")])
    assert payload["metadata"]["reorg_status"] == "orphaned_log"
    assert payload["metadata"]["canonical_block_hash"] == "0xnew"


def test_token_registry_preserves_case_sensitive_solana_mints():
    token = TokenMetadata("solana-mainnet", "AbC123mint", "asset:sol", "TOK", 9)
    registry = TokenRegistry([token])
    assert registry.get("solana-mainnet", "AbC123mint") == token
    assert registry.get("solana-mainnet", "aBc123MINT") is None


def test_address_ranking_excludes_pool_callers_and_non_address_entities():
    now = datetime.now(timezone.utc)
    events = []
    for index in range(2):
        events.append(
            CapitalEvent(
                f"router-{index}",
                "rpc",
                "dex_swap",
                "crypto_spot",
                "uniswap_v3",
                "pool",
                "pool_swap",
                now,
                now,
                1,
                "TOK",
                10_000,
                actor_id="0xrouter",
                metadata={
                    "actor_identity_kind": "evm_pool_caller",
                    "actor_is_pool_caller_not_verified_beneficial_owner": True,
                },
            )
        )
        events.append(
            CapitalEvent(
                f"sec-{index}",
                "sec-edgar",
                "sec_edgar_13f",
                "equities",
                "sec_edgar",
                "CUSIP",
                "13f_holding_report",
                now,
                now,
                100,
                "shares",
                10_000,
                actor_id="sec-cik:123",
                metadata={"actor_identity_kind": "sec_institution"},
            )
        )
    assert rank_capital_candidates(events) == []



def test_ranking_keeps_same_address_separate_across_chains():
    first = event("chain-1-a", "0x" + "a" * 40, 1000, 1, counterparty="")
    second = event("chain-1-b", "0x" + "a" * 40, 2000, 1, counterparty="")
    third = event("chain-137-a", "0x" + "a" * 40, 3000, 1, counterparty="")
    fourth = event("chain-137-b", "0x" + "a" * 40, 4000, 1, counterparty="")
    third = CapitalEvent(
        third.event_id, third.source_id, third.source_kind, third.market, "137",
        third.instrument, third.event_type, third.event_time, third.observed_time,
        third.quantity, third.unit, third.usd_value, actor_id=third.actor_id,
        counterparty_id=third.counterparty_id, confidence=third.confidence,
        metadata=third.metadata,
    )
    fourth = CapitalEvent(
        fourth.event_id, fourth.source_id, fourth.source_kind, fourth.market, "137",
        fourth.instrument, fourth.event_type, fourth.event_time, fourth.observed_time,
        fourth.quantity, fourth.unit, fourth.usd_value, actor_id=fourth.actor_id,
        counterparty_id=fourth.counterparty_id, confidence=fourth.confidence,
        metadata=fourth.metadata,
    )
    ranked = rank_capital_candidates([first, second, third, fourth], minimum_events=2)
    assert {item.entity_id for item in ranked} == {
        "1:" + "0x" + "a" * 40,
        "137:" + "0x" + "a" * 40,
    }
    assert sorted(item.gross_flow_usd for item in ranked) == [3000, 7000]



def test_transfer_ranking_accounts_for_sender_and_recipient_flow():
    sender = event(
        "transfer-1", "wallet:sender", 5000, -1, counterparty="wallet:receiver"
    )
    receiver = rank_capital_candidates([sender], minimum_events=1)
    assert {item.entity_id for item in receiver} == {
        "1:wallet:sender",
        "1:wallet:receiver",
    }
    by_entity = {item.entity_id: item for item in receiver}
    assert by_entity["1:wallet:sender"].net_flow_usd == -5000
    assert by_entity["1:wallet:receiver"].net_flow_usd == 5000
    assert by_entity["1:wallet:receiver"].direction_coverage == 1.0
