"""Unit tests for data-flow observability (schema, registry, debugger, contract)."""

from __future__ import annotations

import time

from aitos.forensics.data_contract import check_orderbook_payload, check_trade_payload
from aitos.forensics.data_flow_debugger import (
    build_data_flow_report,
    find_pressure_point,
    render_text_report,
)
from aitos.forensics.data_flow_registry import DataFlowRegistry
from aitos.forensics.data_flow_schema import (
    PIPELINE_ORDER,
    DataIssueClass,
    PipelineStage,
    make_trace_id,
    parse_trace_id,
)


def test_trace_id_roundtrip() -> None:
    tid = make_trace_id("btcusdt", 12345)
    assert tid == "md:BTCUSDT:12345"
    assert parse_trace_id(tid) == ("BTCUSDT", "12345")
    assert make_trace_id("", 1) is None
    assert parse_trace_id("bad") is None


def test_registry_rates_and_percentiles() -> None:
    reg = DataFlowRegistry(rate_window_seconds=5.0)
    for i in range(10):
        reg.record_input(PipelineStage.RECEIVE, symbol="BTCUSDT")
        reg.record_output(
            PipelineStage.RECEIVE, symbol="BTCUSDT", latency_ms=float(i + 1)
        )
    snap = reg.snapshot_stage(PipelineStage.RECEIVE, "BTCUSDT")
    assert snap.input_count == 10
    assert snap.output_count == 10
    assert snap.latency_p50_ms is not None
    assert snap.latency_max_ms == 10.0
    assert snap.latency_p99_ms is not None


def test_pressure_point_detection() -> None:
    reg = DataFlowRegistry(rate_window_seconds=5.0)
    for _ in range(50):
        reg.record_input(PipelineStage.REDIS_PUBLISH, symbol="ETHUSDT")
    reg.set_queue(
        PipelineStage.REDIS_PUBLISH,
        depth=40,
        oldest_event_age_ms=500,
        symbol="ETHUSDT",
    )
    stages = reg.pipeline_snapshot("ETHUSDT")
    pp = find_pressure_point(stages)
    assert pp is not None
    assert pp["stage"] == PipelineStage.REDIS_PUBLISH.value


def test_debugger_report_shape() -> None:
    reg = DataFlowRegistry()
    reg.record_input(PipelineStage.DECODE, symbol="BTCUSDT")
    reg.record_output(PipelineStage.DECODE, symbol="BTCUSDT", latency_ms=2.5)
    report = build_data_flow_report("btcusdt", registry=reg)
    assert report["symbol"] == "BTCUSDT"
    assert len(report["stages"]) >= 5
    text = render_text_report(report)
    assert "BTCUSDT DATA FLOW" in text
    assert "decode" in text


def test_trade_contract_valid_and_stale() -> None:
    now = time.time() * 1000
    ok = check_trade_payload(
        {"symbol": "BTCUSDT", "price": 100.0, "quantity": 0.1, "T": now - 100},
        now_ms=now,
    )
    assert ok.ok and ok.issue == DataIssueClass.NONE

    stale = check_trade_payload(
        {"s": "ETHUSDT", "p": "200", "q": "1", "E": now - 60_000},
        now_ms=now,
        stale_after_ms=15_000,
    )
    assert stale.ok
    assert stale.issue == DataIssueClass.STALE

    bad = check_trade_payload({"symbol": "X", "price": -1, "quantity": 1})
    assert not bad.ok
    assert bad.issue == DataIssueClass.SEMANTIC_INVALID


def test_orderbook_contract() -> None:
    assert check_orderbook_payload({"symbol": "BTCUSDT", "bids": [], "asks": []}).ok
    assert not check_orderbook_payload({"bids": []}).ok


def test_health_metrics_match_dashboard_contract() -> None:
    from aitos.health_server import HealthServer

    server = HealthServer([])
    lines: list[str] = []
    server._append_data_flow_metrics(lines)
    metrics = "\n".join(lines)
    for name in (
        "aitos_data_flow_input_rate",
        "aitos_data_flow_output_rate",
        "aitos_data_flow_latency_p50_ms",
        "aitos_data_flow_latency_p95_ms",
        "aitos_data_flow_latency_p99_ms",
        "aitos_data_flow_queue_depth",
        "aitos_data_flow_oldest_event_age_ms",
        "aitos_data_flow_source_age_ms",
        "aitos_data_flow_drop_count",
        "aitos_data_flow_error_count",
        "aitos_data_flow_pressure",
        "aitos_data_flow_issue_count",
        "aitos_data_flow_symbol_source_age_ms",
    ):
        assert name in metrics


def test_canonical_market_event_trace_id_and_persistence_stage() -> None:
    from datetime import datetime, timezone

    from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource

    event = MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        market="futures",
        symbol="btcusdt",
        event_time=datetime.now(timezone.utc),
        payload={"price": 100.0, "quantity": 0.1},
        source=MarketSource.WEBSOCKET,
        event_id="trade-123",
    )
    assert event.trace_id == "md:BTCUSDT:trade-123"
    assert PipelineStage.PERSISTENCE in PIPELINE_ORDER


def test_trace_timeline_keeps_event_and_parent_identity() -> None:
    reg = DataFlowRegistry()
    reg.trace_for_event(
        trace_id="md:BTCUSDT:123",
        event_id="123",
        parent_trace_id="md:BTCUSDT:root",
        stage=PipelineStage.REDIS_PUBLISH,
        symbol="BTCUSDT",
        latency_ms=1.5,
    )
    trace = reg.recent_traces(limit=1)[0]
    assert trace["trace_id"] == "md:BTCUSDT:123"
    assert trace["event_id"] == "123"
    assert trace["parent_trace_id"] == "md:BTCUSDT:root"


def test_symbol_freshness_is_tracked_independently() -> None:
    reg = DataFlowRegistry()
    reg.record_input(
        PipelineStage.NORMALIZE,
        symbol="BTCUSDT",
        source_age_ms=42.0,
    )
    assert "BTCUSDT" in reg.symbols()
    assert reg.snapshot_stage(PipelineStage.NORMALIZE, "BTCUSDT").source_age_ms == 42.0


def test_legacy_symbol_only_trace_is_upgraded() -> None:
    from datetime import datetime, timezone

    from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource

    event = MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        market="futures",
        symbol="BTCUSDT",
        event_time=datetime.now(timezone.utc),
        payload={"price": 100.0, "quantity": 0.1},
        source=MarketSource.WEBSOCKET,
        event_id="legacy-42",
        trace_id="BTCUSDT",
    )
    assert event.trace_id == "md:BTCUSDT:legacy-42"
