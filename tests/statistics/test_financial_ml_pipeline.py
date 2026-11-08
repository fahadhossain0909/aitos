from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from aitos.statistics.decision_ledger import DecisionOutcomeLedger
from aitos.statistics.financial_ml import MarketEvent
from aitos.statistics.financial_ml_pipeline import FinancialMLPipeline


def _event(second, price=10.0, quantity=1.0, signed=1.0):
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=second)
    return MarketEvent(stamp.isoformat(), price, quantity, signed)


def _features(score):
    return {"composite_score": score, "trend_strength": score}


def test_event_bar_partial_state_survives_restart(tmp_path):
    path = tmp_path / "financial-ml.sqlite3"
    pipeline = FinancialMLPipeline(
        path,
        volume_bar_threshold=2,
        dollar_bar_threshold=20,
        imbalance_bar_threshold=2,
    )
    assert pipeline.ingest_market_event("BTCUSDT", _event(0)) == {
        "volume": 0,
        "dollar": 0,
        "imbalance": 0,
    }
    pipeline.close()

    restored = FinancialMLPipeline(
        path,
        volume_bar_threshold=2,
        dollar_bar_threshold=20,
        imbalance_bar_threshold=2,
    )
    assert restored.ingest_market_event("BTCUSDT", _event(1)) == {
        "volume": 1,
        "dollar": 1,
        "imbalance": 1,
    }
    assert restored.snapshot()["event_bars"] == 3
    restored.close()


def test_rejected_candidate_gets_counterfactual_barrier_label_and_provenance(tmp_path):
    path = tmp_path / "financial-ml.sqlite3"
    pipeline = FinancialMLPipeline(path)
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert pipeline.register_candidate(
        candidate_id="candidate-counterfactual-001",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100,
        volatility=0.01,
        features=_features(8),
        created_at=created,
    )
    assert pipeline.reject_candidate(
        "candidate-counterfactual-001", "scanner_top_n_rejection"
    )
    assert (
        pipeline.observe_price("BTCUSDT", 102.5, created + timedelta(seconds=10)) == 1
    )
    row = pipeline._conn.execute(
        "SELECT status,label,label_resolution_time,outcome_provenance,outcome_json "
        "FROM financial_ml_candidates WHERE candidate_id=?",
        ("candidate-counterfactual-001",),
    ).fetchone()
    assert row["status"] == "RESOLVED"
    assert row["label"] == 1
    assert row["label_resolution_time"] is not None
    assert row["outcome_provenance"] == "COUNTERFACTUAL_LIVE_MARKOUT"
    assert '"counterfactual": true' in row["outcome_json"]
    pipeline.close()


def test_executed_candidate_is_not_resolved_from_counterfactual_ticks(tmp_path):
    pipeline = FinancialMLPipeline(tmp_path / "financial-ml.sqlite3")
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate_id = "candidate-executed-001"
    pipeline.register_candidate(
        candidate_id=candidate_id,
        symbol="ETHUSDT",
        side="SHORT",
        entry_price=100,
        volatility=0.01,
        features=_features(7),
        created_at=created,
    )
    assert pipeline.mark_executed(candidate_id, "trade-paper-1")
    assert pipeline.observe_price("ETHUSDT", 97, created + timedelta(seconds=30)) == 0
    assert pipeline.resolve_executed(
        candidate_id,
        net_return=0.012,
        resolved_at=created + timedelta(minutes=5),
        trade_id="trade-paper-1",
        outcome={"fees_paid_usd": 0.02, "funding_pnl_usd": -0.001},
    )
    row = pipeline._conn.execute(
        "SELECT status,net_return,outcome_provenance,label_resolution_time,outcome_json "
        "FROM financial_ml_candidates WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    assert row["status"] == "RESOLVED"
    assert row["net_return"] == 0.012
    assert row["outcome_provenance"] == "EXECUTED_TRADE_NET_OUTCOME"
    assert row["label_resolution_time"] is not None
    assert '"counterfactual": false' in row["outcome_json"]
    pipeline.close()


def test_walk_forward_training_keeps_artifact_inactive_until_operator_approval(
    tmp_path,
):
    database = tmp_path / "financial-ml.sqlite3"
    pipeline = FinancialMLPipeline(
        database,
        min_training_samples=50,
        min_oos_samples=20,
    )
    observer = FinancialMLPipeline(database, mode="shadow")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(120):
        candidate_id = f"candidate-{index:03d}"
        score = 10.0 if index % 2 else 0.0
        created = start + timedelta(minutes=20 * index)
        pipeline.register_candidate(
            candidate_id=candidate_id,
            symbol="BTCUSDT",
            side="LONG",
            entry_price=100,
            volatility=0.01,
            features=_features(score),
            created_at=created,
        )
        trade_id = f"trade-{index:03d}"
        resolved_at = created + timedelta(seconds=5)
        if index % 4 in (0, 1):
            pipeline.mark_executed(candidate_id, trade_id)
            pipeline.resolve_executed(
                candidate_id,
                net_return=0.01 if score else -0.01,
                resolved_at=resolved_at,
                trade_id=trade_id,
            )
        else:
            pipeline.reject_candidate(candidate_id, "synthetic_counterfactual")
            pipeline.observe_price(
                "BTCUSDT",
                102.5 if score else 98.0,
                resolved_at,
            )

    report = pipeline.train_walk_forward()
    assert report.trained
    assert report.folds >= 2
    assert report.oos_samples >= 20
    assert report.executed_oos_samples > 0
    assert report.counterfactual_oos_samples > 0
    # Even an OOS-passing report is not auto-promoted.
    active = pipeline._conn.execute(
        "SELECT COUNT(*) FROM financial_ml_artifacts WHERE active=1"
    ).fetchone()[0]
    assert active == 0
    if report.passed_oos:
        artifact_id = pipeline._pending_artifact_id
        with pytest.raises(PermissionError):
            pipeline.approve_artifact(
                artifact_id,
                approved_by="test-operator",
                rationale="Approved after reviewing chronological OOS evidence.",
            )
        assert pipeline.approve_artifact(
            artifact_id,
            approved_by="test-operator",
            rationale="Approved after reviewing chronological OOS evidence.",
            approval_verifier=lambda artifact, operator, reason: (
                artifact == artifact_id
                and operator == "test-operator"
                and len(reason) >= 8
            ),
        )
        assert pipeline.snapshot()["active_oos_approved_artifacts"] == 1
        refreshed = observer.predict("candidate-live-refresh", _features(9))
        assert refreshed.calibrated_probability is not None
    pipeline.close()
    observer.close()


def test_enforce_mode_fails_closed_without_approved_artifact(tmp_path):
    pipeline = FinancialMLPipeline(
        tmp_path / "financial-ml.sqlite3",
        mode="enforce",
    )
    prediction = pipeline.predict("candidate-untrained", _features(9))
    assert prediction.calibrated_probability is None
    assert pipeline.should_block(prediction)
    pipeline.close()


def test_counterfactual_labels_are_available_for_exact_ledger_sync(tmp_path):
    pipeline = FinancialMLPipeline(tmp_path / "financial-ml.sqlite3")
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate_id = "candidate-ledger-sync-001"
    pipeline.register_candidate(
        candidate_id=candidate_id,
        symbol="SOLUSDT",
        side="LONG",
        entry_price=100,
        volatility=0.01,
        features=_features(8),
        created_at=created,
    )
    pipeline.reject_candidate(candidate_id, "meta_veto")
    pipeline.observe_price("SOLUSDT", 102.5, created + timedelta(seconds=9))
    labels = pipeline.pending_counterfactual_labels()
    assert len(labels) == 1
    assert labels[0]["candidate_id"] == candidate_id
    assert (
        labels[0]["label_resolution_time"]
        == (created + timedelta(seconds=9)).isoformat()
    )
    assert labels[0]["outcome_provenance"] == "COUNTERFACTUAL_LIVE_MARKOUT"
    assert pipeline.mark_ledger_synced(candidate_id)
    assert pipeline.pending_counterfactual_labels() == ()
    pipeline.close()


def test_counterfactual_barrier_resolution_syncs_candidate_ledger_exactly(tmp_path):
    database = tmp_path / "financial-ml.sqlite3"
    pipeline = FinancialMLPipeline(database)
    ledger = DecisionOutcomeLedger(tmp_path / "decision-ledger.sqlite3")
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate_id = "candidate-exact-ledger-001"
    pipeline.register_candidate(
        candidate_id=candidate_id,
        symbol="BNBUSDT",
        side="SHORT",
        entry_price=100,
        volatility=0.01,
        features=_features(9),
        created_at=created,
    )
    ledger.record_decision(
        "candidate-gate",
        entity_id=candidate_id,
        symbol="BNBUSDT",
        side="SHORT",
        decision_type="GATE",
        accepted=False,
        reason="meta_shadow_rejection",
        candidate_id=candidate_id,
    )
    pipeline.reject_candidate(candidate_id, "meta_shadow_rejection")
    resolution_time = created + timedelta(seconds=17)
    pipeline.observe_price("BNBUSDT", 97.5, resolution_time)
    label = pipeline.pending_counterfactual_labels()[0]
    assert (
        ledger.resolve_entity(
            candidate_id,
            outcome_value=label["net_return"],
            outcome_at=label["label_resolution_time"],
            outcome=label["outcome"],
            candidate_id=candidate_id,
            outcome_provenance=label["outcome_provenance"],
        )
        == 1
    )
    assert pipeline.mark_ledger_synced(candidate_id)
    decision = ledger.list_decisions(entity_id=candidate_id)[0]
    assert decision["label_resolution_time"] == resolution_time.isoformat()
    assert decision["outcome_provenance"] == "COUNTERFACTUAL_LIVE_MARKOUT"
    assert decision["candidate_id"] == candidate_id
    pipeline.close()


def test_executed_fill_supersedes_unresolved_rejection_before_label_resolution(
    tmp_path,
):
    pipeline = FinancialMLPipeline(tmp_path / "financial-ml.sqlite3")
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate_id = "candidate-retry-execution-001"
    pipeline.register_candidate(
        candidate_id=candidate_id,
        symbol="XRPUSDT",
        side="LONG",
        entry_price=1,
        volatility=0.01,
        features=_features(6),
        created_at=created,
    )
    assert pipeline.reject_candidate(candidate_id, "temporary_prevalidation_reject")
    assert pipeline.mark_executed(candidate_id, "trade-after-reconcile")
    assert pipeline.observe_price("XRPUSDT", 1.03, created + timedelta(seconds=10)) == 0
    row = pipeline._conn.execute(
        "SELECT status,trade_id FROM financial_ml_candidates WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    assert row["status"] == "EXECUTED"
    assert row["trade_id"] == "trade-after-reconcile"
    pipeline.close()


def test_candidate_snapshot_keeps_primary_direction_as_meta_feature(tmp_path):
    pipeline = FinancialMLPipeline(tmp_path / "financial-ml.sqlite3")
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for candidate_id, side, expected in (
        ("candidate-long-direction", "LONG", 1.0),
        ("candidate-short-direction", "SHORT", -1.0),
    ):
        pipeline.register_candidate(
            candidate_id=candidate_id,
            symbol="BTCUSDT",
            side=side,
            entry_price=100,
            volatility=0.01,
            features=_features(5),
            created_at=created,
        )
        row = pipeline._conn.execute(
            "SELECT features_json FROM financial_ml_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        import json

        assert json.loads(row["features_json"])["side_sign"] == expected
    pipeline.close()


def test_candidate_feature_snapshot_is_immutable_after_registration(tmp_path):
    pipeline = FinancialMLPipeline(tmp_path / "financial-ml.sqlite3")
    pipeline.register_candidate(
        candidate_id="candidate-immutable-001",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100,
        volatility=0.01,
        features=_features(5),
    )
    with pytest.raises(
        sqlite3.IntegrityError, match="candidate snapshots are immutable"
    ):
        pipeline._conn.execute(
            "UPDATE financial_ml_candidates SET features_json='{}' "
            "WHERE candidate_id=?",
            ("candidate-immutable-001",),
        )
    pipeline.close()


def test_legacy_candidate_expiry_is_migrated_before_immutability_trigger(tmp_path):
    path = tmp_path / "legacy-financial-ml.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE financial_ml_candidates (
            candidate_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
            created_at TEXT NOT NULL, entry_price REAL NOT NULL, volatility REAL NOT NULL,
            take_price REAL NOT NULL, stop_price REAL NOT NULL, expires_at REAL NOT NULL,
            features_json TEXT NOT NULL, trace_id TEXT, status TEXT NOT NULL DEFAULT 'PENDING',
            label INTEGER, gross_return REAL, net_return REAL, label_resolution_time TEXT,
            outcome_provenance TEXT, trade_id TEXT, outcome_json TEXT
        )"""
    )
    connection.execute(
        """INSERT INTO financial_ml_candidates
        (candidate_id,symbol,side,created_at,entry_price,volatility,take_price,
         stop_price,expires_at,features_json,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "candidate-legacy-expiry",
            "BTCUSDT",
            "LONG",
            "2026-01-01T00:00:00+00:00",
            100.0,
            0.01,
            102.0,
            99.0,
            1767226500.0,
            "{}",
            "PENDING",
        ),
    )
    connection.commit()
    connection.close()

    pipeline = FinancialMLPipeline(path)
    row = pipeline._conn.execute(
        "SELECT expires_at,ledger_synced FROM financial_ml_candidates "
        "WHERE candidate_id=?",
        ("candidate-legacy-expiry",),
    ).fetchone()
    assert isinstance(row["expires_at"], str)
    assert row["ledger_synced"] == 0
    with pytest.raises(
        sqlite3.IntegrityError, match="candidate snapshots are immutable"
    ):
        pipeline._conn.execute(
            "UPDATE financial_ml_candidates SET expires_at=? WHERE candidate_id=?",
            ("2026-01-01T00:00:00+00:00", "candidate-legacy-expiry"),
        )
    pipeline.close()
