"""Minimal Prometheus and health endpoints for the optional WIATS worker."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping, Sequence

from aiohttp import web


def render_prometheus_metrics(
    metrics: Mapping[str, int],
    source_summaries: Sequence[Mapping[str, Any]] = (),
) -> str:
    names = {
        "pages_succeeded": "aitos_wiats_pages_succeeded_total",
        "events_persisted": "aitos_wiats_events_persisted_total",
        "events_published": "aitos_wiats_events_published_total",
        "pages_failed": "aitos_wiats_pages_failed_total",
        "dead_letters_written": "aitos_wiats_dead_letters_written_total",
        "dead_letters_replayed": "aitos_wiats_dead_letters_replayed_total",
        "dead_letters_exhausted": "aitos_wiats_dead_letters_exhausted_total",
        "dead_letters_quarantined": "aitos_wiats_dead_letters_quarantined_total",
        "coordination_candidates": "aitos_wiats_coordination_candidates_total",
        "lease_contention": "aitos_wiats_lease_contention_total",
        "lease_lost": "aitos_wiats_lease_lost_total",
    }
    lines = []
    for key, metric_name in names.items():
        value = int(metrics.get(key, 0))
        if value < 0:
            raise ValueError("Prometheus counters must be non-negative")
        lines.extend(
            (
                f"# TYPE {metric_name} counter",
                f"{metric_name} {value}",
            )
        )
    source_gauges = {
        "runs": "aitos_wiats_source_runs_24h",
        "failures": "aitos_wiats_source_failures_24h",
        "events": "aitos_wiats_source_events_24h",
        "lease_contention": "aitos_wiats_source_lease_contention_24h",
        "mean_latency_ms": "aitos_wiats_source_mean_latency_ms_24h",
    }
    for field, metric_name in source_gauges.items():
        lines.append(f"# TYPE {metric_name} gauge")
        for summary in source_summaries:
            source_id = json.dumps(str(summary.get("source_id", "unknown")))[1:-1]
            value = float(summary.get(field, 0) or 0)
            if not math.isfinite(value) or value < 0:
                continue
            lines.append(f'{metric_name}{{source_id="{source_id}"}} {value}')
    return "\n".join(lines) + "\n"


async def start_metrics_server(worker, *, host: str = "127.0.0.1", port: int = 8120):
    """Start an internal-only HTTP server; caller owns cleanup via returned runner."""
    if not 1 <= int(port) <= 65535:
        raise ValueError("metrics port must be between 1 and 65535")

    async def health(_request):
        return web.json_response(
            {
                "status": "ok",
                "service": "aitos-world-intelligence",
                "metrics": worker.metrics_snapshot(),
            }
        )

    async def sources(_request):
        return web.json_response(
            {
                "lookback_hours": 24,
                "sources": await worker.source_run_summary(lookback_hours=24),
            }
        )

    async def metrics(_request):
        source_summaries = await worker.source_run_summary(lookback_hours=24)
        return web.Response(
            text=render_prometheus_metrics(
                worker.metrics_snapshot(), source_summaries=source_summaries
            ),
            content_type="text/plain",
            charset="utf-8",
        )

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/sources", sources)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, int(port))
    await site.start()
    return runner


__all__ = ["render_prometheus_metrics", "start_metrics_server"]
