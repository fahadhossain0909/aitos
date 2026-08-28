import json
from pathlib import Path

from scripts.redis_stream_archive import ArchiveWriter, id_lt, maxlen_for


def test_stream_ids_use_numeric_order() -> None:
    assert id_lt("100-9", "100-10")
    assert not id_lt("101-0", "100-999")


def test_archive_replay_truncates_uncheckpointed_bytes(tmp_path: Path) -> None:
    writer = ArchiveWriter(root=tmp_path)
    cursors = {}
    writer.append_and_checkpoint("stream:test", [("1-0", {"v": "a"})], cursors)
    path = tmp_path / "test" / "archive.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"stream_id": "2-0", "fields": {"v": "crash"}}) + "\n")
    recovered = writer.recover(writer.load())
    assert recovered["stream:test"]["id"] == "1-0"
    assert path.stat().st_size == recovered["stream:test"]["offset"]


def test_known_and_unknown_streams_are_bounded() -> None:
    assert maxlen_for("stream:market.trade.BTCUSDT") == 25_000
    assert maxlen_for("stream:market.orderbook.BTCUSDT") == 25_000
    assert maxlen_for("stream:market.liquidity.BTCUSDT") == 100_000
    assert maxlen_for("stream:decision.generated") == 10_000
    assert maxlen_for("stream:dlq") == 25_000
    assert maxlen_for("stream:future.topic") == 5_000
