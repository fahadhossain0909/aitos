import pytest

from aitos.world_intelligence.metrics_server import render_prometheus_metrics


def test_wiats_metrics_are_exported_as_prometheus_counters():
    rendered = render_prometheus_metrics(
        {
            "pages_succeeded": 12,
            "events_persisted": 42,
            "pages_failed": 2,
            "dead_letters_written": 1,
            "lease_lost": 0,
        }
    )
    assert "aitos_wiats_pages_succeeded_total 12" in rendered
    assert "aitos_wiats_events_persisted_total 42" in rendered
    assert "aitos_wiats_dead_letters_written_total 1" in rendered


def test_wiats_metrics_reject_negative_counters():
    with pytest.raises(ValueError, match="non-negative"):
        render_prometheus_metrics({"pages_failed": -1})


def test_wiats_source_health_is_exported_as_escaped_prometheus_gauges():
    rendered = render_prometheus_metrics(
        {},
        source_summaries=[
            {
                "source_id": 'sec"edgar',
                "runs": 5,
                "failures": 1,
                "events": 12,
                "lease_contention": 2,
                "mean_latency_ms": 18.5,
            }
        ],
    )
    assert 'aitos_wiats_source_runs_24h{source_id="sec\\"edgar"} 5.0' in rendered
    assert 'aitos_wiats_source_failures_24h{source_id="sec\\"edgar"} 1.0' in rendered
    assert 'aitos_wiats_source_events_24h{source_id="sec\\"edgar"} 12.0' in rendered
