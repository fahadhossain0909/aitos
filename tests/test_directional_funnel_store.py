from aitos.intelligence.opportunity_outcomes import DirectionalFunnelStore


def test_directional_funnel_persists_and_aggregates_gate_events(tmp_path):
    path = tmp_path / "directional-funnel.sqlite3"
    store = DirectionalFunnelStore(path)
    store.record(
        symbol="BTCUSDT",
        side="LONG",
        stage="directional_gate",
        accepted=True,
        reason="accepted",
        details={"cvd_score": 8.0},
    )
    store.record(
        symbol="BTCUSDT",
        side="SHORT",
        stage="directional_gate",
        accepted=False,
        reason="cvd_below_directional_threshold",
        details={"cvd_score": 8.0},
    )
    store.record(
        symbol="BTCUSDT",
        side="LONG",
        stage="mtf_gate",
        accepted=False,
        reason="countertrend_without_confirmed_reversal",
    )
    before_close = store.snapshot()
    assert before_close["events_retained"] == 3
    assert (
        before_close["stages"]["directional_gate"]["by_side"]["LONG"]["accepted"] == 1
    )
    assert (
        before_close["stages"]["directional_gate"]["by_side"]["SHORT"]["rejected"] == 1
    )
    store.close()

    reopened = DirectionalFunnelStore(path)
    after_reopen = reopened.snapshot()
    assert after_reopen == before_close
    assert after_reopen["stages"]["mtf_gate"]["by_side"]["LONG"]["rejected"] == 1
    reopened.close()


def test_directional_funnel_rejects_unknown_side_and_missing_stage(tmp_path):
    store = DirectionalFunnelStore(tmp_path / "funnel.sqlite3")
    try:
        import pytest

        with pytest.raises(ValueError, match="side"):
            store.record(
                symbol="BTCUSDT",
                side="BUY",
                stage="directional_gate",
                accepted=False,
            )
        with pytest.raises(ValueError, match="symbol and stage"):
            store.record(
                symbol="BTCUSDT",
                side="LONG",
                stage="",
                accepted=False,
            )
    finally:
        store.close()
