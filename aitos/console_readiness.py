"""Explainable operator decision support, not a trading or live-approval brain.

Only present-tense observations from the console snapshot may pass a check.
Unattached services, historical test claims and unmeasured production gates never
become healthy by inference. No aggregate score can override a safety blocker.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone


def assess_readiness(snapshot: dict, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    gates = []

    def gate(key, title, status, evidence, action, priority="P1"):
        gates.append(
            dict(
                id=key,
                title=title,
                status=status,
                evidence=evidence,
                next_action=action,
                priority=priority,
            )
        )

    attached = snapshot.get("connected") is True
    gate(
        "runtime",
        "Runtime attachment",
        "passed" if attached else "blocked",
        "Runtime modules attached." if attached else "No runtime modules attached.",
        "Attach an approved paper runtime through the private read-only bridge.",
        "P0",
    )
    modules = snapshot.get("modules", [])
    unhealthy = [
        m.get("name", "unknown") for m in modules if m.get("status") != "healthy"
    ]
    status = (
        "not_measured"
        if not attached or not modules
        else "blocked" if unhealthy else "passed"
    )
    gate(
        "health",
        "Module health observations",
        status,
        (
            f"{len(unhealthy)} non-healthy modules out of {len(modules)} observed."
            if attached
            else "Module health is not measured."
        ),
        "Inspect module failures and obtain recovery evidence; health alone does not prove end-to-end correctness.",
        "P0",
    )
    age = None
    try:
        generated = datetime.fromisoformat(
            snapshot["generated_at"].replace("Z", "+00:00")
        )
        if generated.utcoffset() is not None:
            age = (now - generated).total_seconds()
    except (KeyError, ValueError, TypeError, AttributeError):
        pass
    fresh = age is not None and 0 <= age <= 15
    gate(
        "freshness",
        "Snapshot freshness",
        "passed" if attached and fresh else "blocked" if attached else "not_measured",
        (
            f"Server snapshot age: {age:.2f} seconds."
            if age is not None
            else "No valid timezone-aware snapshot timestamp."
        ),
        "Restore the bridge and check clocks; stale snapshots are not current financial evidence.",
        "P0",
    )
    stages = snapshot.get("pipeline", [])
    measured = [
        s
        for s in stages
        if isinstance(s.get("latency_sample_count"), int)
        and s["latency_sample_count"] > 0
        and isinstance(s.get("latency_p95_ms"), (int, float))
        and math.isfinite(s["latency_p95_ms"])
        and s["latency_p95_ms"] >= 0
    ]
    gate(
        "samples",
        "Stage latency sample coverage",
        (
            "passed"
            if attached and stages and len(measured) == len(stages)
            else "not_measured"
        ),
        f"{len(measured)} / {len(stages)} stages have finite p95 observations. No latency SLO inferred.",
        "Collect representative samples from every stage, then verify event-to-execution latency separately.",
    )
    errors = sum(max(0, int(s.get("error_count", 0))) for s in stages)
    gate(
        "errors",
        "Observed stage error counters",
        (
            "blocked"
            if attached and errors
            else "passed" if attached and measured else "not_measured"
        ),
        f"{errors} cumulative process-local stage errors; this is not an error rate.",
        "Inspect error windows and replay failures; a zero counter without traffic proves nothing.",
        "P0",
    )
    gate(
        "surface",
        "Read-only console contract",
        "passed" if snapshot.get("controls") == "read_only" else "blocked",
        "Snapshot declares read-only controls. This is not an independent security audit.",
        "Keep runtime mutations behind a separately reviewed authenticated control plane.",
        "P0",
    )
    for key, title, evidence, action in (
        (
            "history",
            "Real-service persistence recovery",
            (
                "History adapter configured; restart/revision integrity is unverified."
                if snapshot.get("history_available")
                else "No history adapter attached."
            ),
            "Verify ClickHouse writes, ordered revisions and process-restart restoration against a real isolated service.",
        ),
        (
            "recovery",
            "Account and protective-order recovery",
            "No current account/child-fill reconciliation certificate is supplied.",
            "Compare scoped journal and open-algo evidence; independently reconcile positions and terminal child fills.",
        ),
        (
            "soak",
            "Paper soak and end-to-end latency",
            "No dated paper-soak or production latency evidence is supplied.",
            "Run isolated paper soak with replayable event traces, bounded loss/error windows and measured percentiles.",
        ),
        (
            "security",
            "Operator authentication and native delivery",
            "Private dev proxy is not production operator authentication or a signed installer.",
            "Review operator sessions, credential vault, origin isolation, signed packaging and OS recovery.",
        ),
    ):
        gate(key, title, "not_measured", evidence, action, "P0")
    passed = sum(g["status"] == "passed" for g in gates)
    blocked = sum(g["status"] == "blocked" for g in gates)
    return dict(
        schema_version=1,
        engine="deterministic-evidence-rules-v1",
        scope="console_observations_not_production_certification",
        generated_at=now.isoformat(),
        live_authorized=False,
        verdict="blocked" if blocked else "not_certified",
        counts=dict(
            passed=passed,
            blocked=blocked,
            not_measured=len(gates) - passed - blocked,
            total=len(gates),
        ),
        gates=gates,
        next_actions=[
            dict(gate_id=g["id"], priority=g["priority"], action=g["next_action"])
            for g in sorted(
                gates, key=lambda g: (g["status"] != "blocked", g["priority"], g["id"])
            )
            if g["status"] != "passed"
        ],
        disclaimer="Decision support only. No model, score or healthy endpoint authorizes live trading.",
    )
