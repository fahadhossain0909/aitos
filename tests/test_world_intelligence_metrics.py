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
