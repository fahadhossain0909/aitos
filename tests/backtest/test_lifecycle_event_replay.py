from __future__ import annotations

import json

import pytest

from aitos.backtest.lifecycle_event_replay import LifecycleEventReplay


@pytest.mark.asyncio
async def test_lifecycle_replay_orders_events_and_persists_deduplication(tmp_path):
    path = tmp_path / "account-events.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "event-b",
                        "timestamp_ms": 200,
                        "event_type": "ACCOUNT_UPDATE",
                        "symbol": "BTCUSDT",
                        "payload": {"wallet": "100"},
                    }
                ),
                json.dumps(
                    {
                        "event_id": "event-a",
                        "timestamp_ms": 100,
                        "event_type": "ORDER_TRADE_UPDATE",
                        "symbol": "BTCUSDT",
                        "payload": {"status": "FILLED"},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    checkpoint = tmp_path / "replay.sqlite"
    observed = []
    events = LifecycleEventReplay.read_jsonl(path)
    first_runner = LifecycleEventReplay(
        lambda event: observed.append(event.event_id),
        checkpoint_path=checkpoint,
    )
    first = await first_runner.replay(events)
    second_runner = LifecycleEventReplay(
        lambda event: observed.append(event.event_id),
        checkpoint_path=checkpoint,
    )
    second = await second_runner.replay(events)

    assert observed == ["event-a", "event-b"]
    assert first == {"seen": 2, "processed": 2, "duplicates": 0}
    assert second == {"seen": 2, "processed": 0, "duplicates": 2}


@pytest.mark.asyncio
async def test_failed_replay_handler_does_not_advance_checkpoint(tmp_path):
    path = tmp_path / "retry.sqlite"
    event = LifecycleEventReplay.read_jsonl(
        _write_event_file(tmp_path / "single.jsonl")
    )[0]

    async def failing(_event):
        raise RuntimeError("projection failed")

    runner = LifecycleEventReplay(failing, checkpoint_path=path)
    with pytest.raises(RuntimeError, match="projection failed"):
        await runner.replay([event])

    retry_observed = []
    retry = LifecycleEventReplay(
        lambda item: retry_observed.append(item.event_id),
        checkpoint_path=path,
    )
    report = await retry.replay([event])
    assert report["processed"] == 1
    assert retry_observed == [event.event_id]


def _write_event_file(path):
    path.write_text(
        json.dumps(
            {
                "event_id": "retry-event",
                "timestamp_ms": 123,
                "event_type": "ORDER_TRADE_UPDATE",
                "symbol": "ETHUSDT",
                "payload": {"status": "PARTIALLY_FILLED"},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_lifecycle_replay_normalizes_raw_binance_user_stream_events(tmp_path):
    path = tmp_path / "binance-user-stream.jsonl"
    raw_order = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1700000000123,
        "o": {
            "s": "BTCUSDT",
            "i": 987,
            "c": "aitos-switch-open",
            "X": "FILLED",
            "x": "TRADE",
            "z": "0.25",
            "ap": "40000.0",
        },
    }
    raw_account = {
        "e": "ACCOUNT_UPDATE",
        "E": 1700000000456,
        "a": {"m": "ORDER", "P": [{"s": "BTCUSDT", "pa": "0.25"}]},
    }
    path.write_text(
        "\n".join([json.dumps(raw_order), json.dumps(raw_account)]),
        encoding="utf-8",
    )

    events = LifecycleEventReplay.read_jsonl(path)

    assert len(events) == 2
    assert events[0].event_type == "ORDER_TRADE_UPDATE"
    assert events[0].symbol == "BTCUSDT"
    assert events[0].payload["o"]["X"] == "FILLED"
    assert events[1].event_type == "ACCOUNT_UPDATE"
    assert events[1].symbol == ""
    assert events[0].event_id != events[1].event_id


@pytest.mark.asyncio
async def test_lifecycle_replay_preserves_capture_order_for_equal_exchange_timestamps(
    tmp_path,
):
    path = tmp_path / "same-timestamp.jsonl"
    rows = [
        {
            "event_id": "z-order",
            "timestamp_ms": 100,
            "event_type": "ORDER_TRADE_UPDATE",
            "symbol": "BTCUSDT",
        },
        {
            "event_id": "a-account",
            "timestamp_ms": 100,
            "event_type": "ACCOUNT_UPDATE",
            "symbol": "BTCUSDT",
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )
    observed = []
    replay = LifecycleEventReplay(lambda event: observed.append(event.event_id))

    await replay.replay(LifecycleEventReplay.read_jsonl(path))

    assert observed == ["z-order", "a-account"]
