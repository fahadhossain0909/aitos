from datetime import datetime, timedelta, timezone

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
    assert pipeline.observe_price(
        "BTCUSDT", 102.5, created + timedelta(seconds=10)
    ) == 1
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
    assert pipeline.observe_price(
        "ETHUSDT", 97, created + timedelta(seconds=30)
    ) == 0
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


def test_walk_forward_training_keeps_artifact_inactive_until_operator_approval(tmp_path):
    pipeline = FinancialMLPipeline(
        tmp_path / "financial-ml.sqlite3",
        min_training_samples=50,
        min_oos_samples=20,
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(80):
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
        pipeline.mark_executed(candidate_id, f"trade-{index:03d}")
        pipeline.resolve_executed(
            candidate_id,
            net_return=0.01 if score else -0.01,
            resolved_at=created + timedelta(seconds=5),
            trade_id=f"trade-{index:03d}",
        )

    report = pipeline.train_walk_forward()
    assert report.trained
    assert report.folds >= 2
    assert report.oos_samples >= 20
    # Even an OOS-passing report is not auto-promoted.
    active = pipeline._conn.execute(
        "SELECT COUNT(*) FROM financial_ml_artifacts WHERE active=1"
    ).fetchone()[0]
    assert active == 0
    if report.passed_oos:
        artifact_id = pipeline._pending_artifact_id
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
    pipeline.close()
