from datetime import datetime, timezone, timedelta
import pytest
from aitos.console_readiness import assess_readiness

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def snapshot():
    return dict(
        connected=True,
        generated_at=NOW.isoformat(),
        controls="read_only",
        modules=[dict(name="lifecycle", status="healthy")],
        pipeline=[
            dict(stage="one", latency_sample_count=5, latency_p95_ms=1, error_count=0)
        ],
        history_available=True,
    )


def test_healthy_observations_never_authorize_live():
    report = assess_readiness(snapshot(), now=NOW)
    assert report["live_authorized"] is False
    assert report["verdict"] == "not_certified"
    assert report["counts"] == dict(passed=6, blocked=0, not_measured=4, total=10)
    assert {"history", "recovery", "security", "soak"} == {
        g["id"] for g in report["gates"] if g["status"] == "not_measured"
    }


def test_unattached_adapter_not_a_runtime_certificate():
    s = snapshot()
    s["connected"] = False
    s["modules"] = []
    s["pipeline"] = []
    report = assess_readiness(s, now=NOW)
    assert report["counts"]["blocked"] >= 1
    assert report["next_actions"][0]["gate_id"] == "runtime"


@pytest.mark.parametrize(
    "timestamp",
    [
        "bad",
        "2026-11-07T12:00:00",
        (NOW - timedelta(seconds=16)).isoformat(),
        (NOW + timedelta(seconds=1)).isoformat(),
    ],
)
def test_stale_naive_or_future_snapshot_blocks(timestamp):
    s = snapshot()
    s["generated_at"] = timestamp
    result = assess_readiness(s, now=NOW)
    assert (
        next(g for g in result["gates"] if g["id"] == "freshness")["status"]
        == "blocked"
    )


def test_health_and_stage_errors_remain_separate_blockers():
    s = snapshot()
    s["modules"][0]["status"] = "unavailable"
    s["pipeline"][0]["error_count"] = 2
    blocked = {
        g["id"]
        for g in assess_readiness(s, now=NOW)["gates"]
        if g["status"] == "blocked"
    }
    assert blocked == {"health", "errors"}


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1])
def test_absent_or_invalid_measurements_never_pass(value):
    s = snapshot()
    s["pipeline"][0]["latency_p95_ms"] = value
    gates = assess_readiness(s, now=NOW)["gates"]
    assert next(g for g in gates if g["id"] == "samples")["status"] == "not_measured"
