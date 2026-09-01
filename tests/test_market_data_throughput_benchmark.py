from scripts.market_data_throughput_benchmark import build_events, run


def test_build_events_preserves_requested_volume() -> None:
    events = build_events(1.0, trade_rate=100, book_rate=200)
    assert len(events) == 300
    assert {event.kind for event in events} == {"trade", "book"}


def test_benchmark_reports_processed_events() -> None:
    result = run(seconds=0.1, trade_rate=100, book_rate=200, book_work=1)
    assert result["events"] == 30
    assert result["processed"] == 30
    assert result["throughput_events_per_second"] > 0
    assert result["peak_queue"] >= 1
