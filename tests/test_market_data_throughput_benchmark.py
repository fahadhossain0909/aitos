from scripts.market_data_throughput_benchmark import (
    build_events,
    coalesce_book_events,
    run,
)


def test_build_events_preserves_requested_volume() -> None:
    events = build_events(1.0, trade_rate=100, book_rate=200)
    assert len(events) == 300
    assert {event.kind for event in events} == {"trade", "book"}


def test_coalescing_never_drops_trade_events() -> None:
    events = build_events(1.0, trade_rate=100, book_rate=200)
    coalesced = coalesce_book_events(events, window_ns=10_000_000)
    assert sum(event.kind == "trade" for event in coalesced) == 100
    assert sum(event.kind == "book" for event in coalesced) < 200


def test_benchmark_reports_all_three_models() -> None:
    for model in ("event", "batch", "coalesce"):
        result = run(
            seconds=0.1,
            trade_rate=100,
            book_rate=200,
            book_work=1,
            model=model,
        )
        assert result["arrival_events"] == 30
        assert result["strategy_processed_events"] > 0
        assert result["service_throughput_events_per_second"] > 0
        assert result["peak_queue"] >= 0


def test_batch_keeps_all_trade_events() -> None:
    result = run(
        seconds=0.1,
        trade_rate=100,
        book_rate=200,
        book_work=1,
        model="batch",
    )
    assert result["strategy_trade_updates"] == 10
    assert result["raw_book_events"] == 20


def test_coalesce_reduces_strategy_book_work() -> None:
    result = run(
        seconds=0.1,
        trade_rate=100,
        book_rate=200,
        book_work=1,
        model="coalesce",
        coalesce_ms=10.0,
    )
    assert result["strategy_trade_updates"] == 10
    assert result["raw_book_events"] == 20
    assert result["strategy_book_updates"] < result["raw_book_events"]
