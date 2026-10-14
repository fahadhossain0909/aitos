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
