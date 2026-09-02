import json
from pathlib import Path

import scripts.redis_stream_archive as archive
from scripts.redis_stream_archive import ArchiveWriter, maxlen_for


def test_archive_replay_truncates_uncheckpointed_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(archive, "ROOT", tmp_path)
    monkeypatch.setattr(archive, "CURSOR_FILE", tmp_path / ".cursors.json")
    writer = ArchiveWriter()
    cursors = {}
    writer.append_and_checkpoint("stream:test", [("1-0", {"v": "a"})], cursors)
    path = tmp_path / "test" / "archive.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"stream_id": "2-0", "fields": {"v": "crash"}}) + "\n")
    recovered = writer.recover(writer.load())
    assert recovered["stream:test"]["id"] == "1-0"
    assert path.stat().st_size == recovered["stream:test"]["offset"]


def test_known_and_unknown_streams_are_bounded() -> None:
    assert maxlen_for("stream:market.trade.BTCUSDT") == 10_000
    assert maxlen_for("stream:market.orderbook.BTCUSDT") == 10_000
    assert maxlen_for("stream:market.liquidity.BTCUSDT") == 20_000
    assert maxlen_for("stream:market.live_state.BTCUSDT") == 10_000
    assert maxlen_for("stream:market.orderflow.BTCUSDT") == 10_000
    assert maxlen_for("stream:market.kline.BTCUSDT") == 10_000
    assert maxlen_for("stream:decision.generated") == 10_000
    assert maxlen_for("stream:dlq") == 25_000
    assert maxlen_for("stream:future.topic") == 5_000
