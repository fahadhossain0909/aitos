from scripts.run_capital_intelligence import _render_metrics


def test_cfei_metrics_include_source_health_and_valid_prometheus_lines():
    rendered = _render_metrics(
        {
            "events": 12,
            "errors": 2,
            "last_poll": 1_800_000_000,
            "sources": {
                'evm:1"main': {
                    "events": 10,
                    "errors": 1,
                    "last_success": 1_800_000_000,
                }
            },
        }
    ).decode()
    assert "aitos_cfei_events_persisted 12\n" in rendered
    assert (
        'aitos_cfei_source_events_persisted_total{source_id="evm:1\\"main"} 10'
        in rendered
    )
    assert "aitos_cfei_source_poll_errors_total" in rendered
    assert rendered.endswith("\n")
    assert "\\n" not in rendered
