from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import sqlite3
import stat

import pytest

from aitos.security.paper_processor import (
    PaperExecutionAdapter,
    PaperOnlyIntentProcessor,
)
from aitos.security.replay_store import (
    ReplayConflict,
    ReplayStoreError,
    SQLiteReplayStore,
)
from aitos.security.signing_policy import OrderIntentPolicy, validate_order_intent


NOW = 1_800_000_000_000
POLICY = OrderIntentPolicy(
    account_alias="paper-primary",
    allowed_symbols=frozenset({"BTCUSDT"}),
    max_order_notional=Decimal("1000"),
)


def make_intent(**overrides):
    payload = {
        "schema_version": 1,
        "intent_id": "intent-replay-001",
        "nonce": "AAECAwQFBgcICQoLDA0ODw",
        "issued_at_ms": NOW - 100,
        "expires_at_ms": NOW + 10_000,
        "account_alias": "paper-primary",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "0.001",
        "limit_price": "65000",
        "reduce_only": False,
        "client_order_id": "aitos-replay-001",
    }
    payload.update(overrides)
    return validate_order_intent(payload, policy=POLICY, now_ms=NOW)


def test_reservation_is_durable_across_store_reopen(tmp_path):
    database = tmp_path / "signer-ledger.sqlite3"
    intent = make_intent()

    first = SQLiteReplayStore(database)
    reserved = first.reserve(intent, now_ms=NOW)
    assert reserved.created is True
    assert reserved.status == "reserved"
    first.mark_dispatching(intent.intent_id, now_ms=NOW + 1)
    first.close()

    reopened = SQLiteReplayStore(database)
    duplicate = reopened.reserve(intent, now_ms=NOW + 2)
    assert duplicate.created is False
    assert duplicate.status == "dispatching"
    assert duplicate.fingerprint == reserved.fingerprint
    reopened.close()


def test_nonce_or_intent_identity_reuse_with_different_payload_is_rejected(tmp_path):
    with SQLiteReplayStore(tmp_path / "ledger.sqlite3") as store:
        store.reserve(make_intent(), now_ms=NOW)
        with pytest.raises(ReplayConflict):
            store.reserve(
                make_intent(intent_id="intent-replay-002"),
                now_ms=NOW + 1,
            )
        with pytest.raises(ReplayConflict):
            store.reserve(
                make_intent(symbol="BTCUSDT", client_order_id="other-client-id"),
                now_ms=NOW + 1,
            )


def test_client_order_id_cannot_be_reused_for_another_intent(tmp_path):
    with SQLiteReplayStore(tmp_path / "ledger.sqlite3") as store:
        store.reserve(make_intent(), now_ms=NOW)
        with pytest.raises(ReplayConflict):
            store.reserve(
                make_intent(
                    intent_id="intent-replay-002",
                    nonce="AQECAwQFBgcICQoLDA0ODw",
                ),
                now_ms=NOW + 1,
            )


def test_only_valid_state_transitions_are_allowed(tmp_path):
    with SQLiteReplayStore(tmp_path / "ledger.sqlite3") as store:
        with pytest.raises(ReplayStoreError):
            store.mark_dispatching("missing", now_ms=NOW)
        store.reserve(make_intent(), now_ms=NOW)
        store.mark_dispatching("intent-replay-001", now_ms=NOW + 1)
        store.finish(
            "intent-replay-001",
            status="accepted",
            outcome_code="paper_order_recorded",
            now_ms=NOW + 2,
        )
        with pytest.raises(ReplayStoreError):
            store.mark_dispatching("intent-replay-001", now_ms=NOW + 3)
        assert store.get("intent-replay-001").status == "accepted"


def test_reservation_is_atomic_between_independent_store_instances(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    stores = [SQLiteReplayStore(database), SQLiteReplayStore(database)]
    intent = make_intent()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda store: store.reserve(intent, now_ms=NOW), stores)
            )
        assert sorted(result.created for result in results) == [False, True]
    finally:
        for store in stores:
            store.close()


def test_nonce_is_hashed_and_database_permissions_are_private(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    nonce = "AAECAwQFBgcICQoLDA0ODw"
    with SQLiteReplayStore(database) as store:
        store.reserve(make_intent(), now_ms=NOW)
    connection = sqlite3.connect(database)
    try:
        values = connection.execute(
            "SELECT nonce_sha256 FROM signer_intent_ledger"
        ).fetchone()
    finally:
        connection.close()
    assert nonce not in values[0]
    assert len(values[0]) == 64
    if os.name == "posix":
        assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_in_memory_database_and_symlink_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="durable_database_path_required"):
        SQLiteReplayStore(":memory:")
    target = tmp_path / "real.sqlite3"
    target.touch()
    link = tmp_path / "linked.sqlite3"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is not available on this runner")
    with pytest.raises(ValueError, match="must_not_be_symlink"):
        SQLiteReplayStore(link)


class CountingPaperAdapter(PaperExecutionAdapter):
    def __init__(self):
        self.calls = 0

    def execute(self, intent):
        self.calls += 1
        return super().execute(intent)


class AmbiguousPaperAdapter(PaperExecutionAdapter):
    def __init__(self):
        self.calls = 0

    def execute(self, intent):
        self.calls += 1
        raise TimeoutError("simulated acknowledgement loss")


def test_paper_processor_executes_same_intent_only_once(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    adapter = CountingPaperAdapter()
    intent = make_intent()
    with SQLiteReplayStore(database) as store:
        processor = PaperOnlyIntentProcessor(
            policy=POLICY, replay_store=store, adapter=adapter
        )
        first = processor.process(intent_payload(), now_ms=NOW)
        second = processor.process(intent_payload(), now_ms=NOW + 1)
    assert first.status == "accepted"
    assert first.duplicate is False
    assert second.status == "accepted"
    assert second.duplicate is True
    assert adapter.calls == 1


def intent_payload():
    return {
        "schema_version": 1,
        "intent_id": "intent-replay-001",
        "nonce": "AAECAwQFBgcICQoLDA0ODw",
        "issued_at_ms": NOW - 100,
        "expires_at_ms": NOW + 10_000,
        "account_alias": "paper-primary",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "0.001",
        "limit_price": "65000",
        "reduce_only": False,
        "client_order_id": "aitos-replay-001",
    }


def test_ambiguous_paper_dispatch_is_never_automatically_retried(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    adapter = AmbiguousPaperAdapter()
    with SQLiteReplayStore(database) as store:
        processor = PaperOnlyIntentProcessor(
            policy=POLICY, replay_store=store, adapter=adapter
        )
        first = processor.process(intent_payload(), now_ms=NOW)
    with SQLiteReplayStore(database) as reopened:
        processor = PaperOnlyIntentProcessor(
            policy=POLICY, replay_store=reopened, adapter=adapter
        )
        retry = processor.process(intent_payload(), now_ms=NOW + 1)
    assert first.status == "unknown"
    assert retry.status == "unknown"
    assert retry.duplicate is True
    assert adapter.calls == 1


def test_paper_processor_rejects_market_policy_and_non_paper_adapter(tmp_path):
    market_policy = OrderIntentPolicy(
        account_alias="paper-primary",
        allowed_symbols=frozenset({"BTCUSDT"}),
        max_order_notional=Decimal("1000"),
        allowed_order_types=frozenset({"MARKET"}),
        allow_market_orders=True,
    )
    with SQLiteReplayStore(tmp_path / "market.sqlite3") as store:
        with pytest.raises(ValueError, match="limit_orders_only"):
            PaperOnlyIntentProcessor(policy=market_policy, replay_store=store)

        class NonPaperAdapter:
            paper_only = False

        with pytest.raises(ValueError, match="non_paper_adapter"):
            PaperOnlyIntentProcessor(
                policy=POLICY,
                replay_store=store,
                adapter=NonPaperAdapter(),
            )



def test_replay_store_fails_closed_at_capacity(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    with SQLiteReplayStore(database, max_records=1) as store:
        store.reserve(make_intent(), now_ms=NOW)
        second = make_intent(
            intent_id="intent-replay-002",
            nonce="AQECAwQFBgcICQoLDA0ODw",
            client_order_id="aitos-replay-002",
        )
        with pytest.raises(ReplayStoreError, match="capacity_reached"):
            store.reserve(second, now_ms=NOW + 1)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory mode check")
def test_replay_store_rejects_group_or_world_accessible_parent(tmp_path):
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    os.chmod(private_dir, 0o755)
    with pytest.raises(ValueError, match="directory_must_be_private"):
        SQLiteReplayStore(private_dir / "ledger.sqlite3")
