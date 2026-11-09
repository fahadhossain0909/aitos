import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from aitos.statistics.governance import PromotionEvidence, PromotionPolicy
from aitos.statistics.governance_runtime import StatisticalGovernanceRuntime
from aitos.statistics.production_drift import (
    ClickHouseDriftFeatureBatchProvider,
    OperatorApprovalService,
    OperatorIdentity,
)
from aitos.statistics.registry import StatisticalModelRegistry


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def query_closed_experiences(self, start, end, *, sources, limit):
        return [r for r in self.rows if start <= r["timestamp"] < end]


def test_clickhouse_provider_creates_disjoint_batches_and_fails_closed():
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    rows = []
    for i in range(25):
        rows.append(
            {
                "timestamp": now - timedelta(days=8) + timedelta(minutes=i),
                "features": {"vol": float(i), "ignored": "x"},
            }
        )
        rows.append(
            {
                "timestamp": now - timedelta(days=1) + timedelta(minutes=i),
                "features": {"vol": float(i + 100)},
            }
        )
    provider = ClickHouseDriftFeatureBatchProvider(
        FakeStore(rows),
        lookback=timedelta(days=10),
        window=timedelta(days=2),
        min_observations=20,
        feature_names=("vol",),
        clock=lambda: now,
    )
    reference, current = provider()
    assert len(reference["vol"]) == 25
    assert len(current["vol"]) == 25
    with pytest.raises(RuntimeError, match="insufficient"):
        ClickHouseDriftFeatureBatchProvider(
            FakeStore(rows[:10]),
            lookback=timedelta(days=10),
            window=timedelta(days=2),
            min_observations=20,
            feature_names=("vol",),
            clock=lambda: now,
        )()


def _evidence():
    return PromotionEvidence(
        model_id="a-stat",
        model_version="2.0.0",
        observations=2000,
        shadow_days=45,
        brier=0.18,
        qlike=1.2,
        pbo=0.08,
        max_drawdown=0.05,
        drift_score=0.02,
        paired_oos_improvement_lower_bound=0.01,
        dsr_probability=0.99,
        mcs_included=True,
        reality_check_p_value=0.01,
        dm_p_value=0.01,
        multiple_testing_adjusted_p_value=0.02,
        operator_approved=False,
    )


def _service(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "registry.sqlite3")
    digest = hashlib.sha256(b"2.0.0").hexdigest()
    registry.register_candidate(
        model_id="a-stat",
        model_version="2.0.0",
        artifact_uri="models/2.0.0.bin",
        artifact_sha256=digest,
        config_hash=digest,
        feature_schema_version="1",
    )
    runtime = StatisticalGovernanceRuntime(
        registry, policy=PromotionPolicy(min_observations=100, min_shadow_days=1)
    )
    service = OperatorApprovalService(
        registry,
        runtime,
        database_path=tmp_path / "approvals.sqlite3",
        signing_secret="x" * 40,
        allowed_issuers=("https://issuer.example",),
    )
    identity = OperatorIdentity(
        "https://issuer.example", "operator-1", ("aitos-model-approver",)
    )
    return registry, service, identity


def test_approval_is_authenticated_candidate_bound_and_one_time(tmp_path):
    registry, service, identity = _service(tmp_path)
    token = service.issue_approval(
        identity,
        model_id="a-stat",
        model_version="2.0.0",
        reason="Approve validated challenger after OOS review",
    )
    decision = service.approve_and_promote(identity, token, _evidence())
    assert decision.eligible
    assert registry.get_champion("a-stat").model_version == "2.0.0"
    events = service.audit_history("a-stat")
    assert [event["event"] for event in events] == [
        "ISSUED",
        "CONSUMED",
        "PROMOTION_DECISION",
    ]
    with sqlite3.connect(service.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(
                "UPDATE operator_approval_audit SET event='TAMPERED' WHERE audit_id=1"
            )
    with pytest.raises(PermissionError, match="consumed"):
        service.approve_and_promote(identity, token, _evidence())


def test_approval_rejects_wrong_role_and_candidate_mismatch(tmp_path):
    _, service, identity = _service(tmp_path)
    with pytest.raises(PermissionError):
        service.issue_approval(
            OperatorIdentity(identity.issuer, "viewer", ("viewer",)),
            model_id="a-stat",
            model_version="2.0.0",
            reason="Viewer must not approve any candidate",
        )
    token = service.issue_approval(
        identity,
        model_id="a-stat",
        model_version="2.0.0",
        reason="Approve candidate after independent review",
    )
    bad = _evidence()

    with pytest.raises(PermissionError, match="does not match"):
        service.approve_and_promote(
            identity, token, replace(bad, model_version="3.0.0")
        )
