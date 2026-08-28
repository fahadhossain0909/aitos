from pathlib import Path

import scripts.redis_stream_archive as archive
from scripts.redis_stream_archive import ArchiveWriter, maxlen_for


def test_known_stream_families_are_bounded() -> None:
    assert maxlen_for("stream:market.trade.BTCUSDT") == 25_000
    assert maxlen_for("stream:market.orderbook.BTCUSDT") == 25_000
    assert maxlen_for("stream:market.liquidity.BTCUSDT") == 100_000
    assert maxlen_for("stream:market.orderflow.BTCUSDT") == 25_000
    assert maxlen_for("stream:decision.generated") == 10_000
    assert maxlen_for("stream:journal.decision_recorded") == 10_000
    assert maxlen_for("stream:trade.position_opened") == 10_000
    assert maxlen_for("stream:risk.score_update") == 10_000


def test_unknown_streams_have_a_safe_default_bound() -> None:
    assert maxlen_for("stream:future.new_topic") == 5_000


def test_cursor_checkpoint_is_atomic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(archive, "ROOT", tmp_path)
    monkeypatch.setattr(archive, "CURSOR_FILE", tmp_path / ".cursors.json")
    writer = ArchiveWriter()
    writer.save({"stream:test": "123-0"})
    assert writer.load() == {"stream:test": "123-0"}


def test_archive_contains_stream_id_before_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(archive, "ROOT", tmp_path)
    monkeypatch.setattr(archive, "CURSOR_FILE", tmp_path / ".cursors.json")
    writer = ArchiveWriter()
    cursors = {}
    writer.append_and_checkpoint("stream:test", [("1-0", {"value": "ok"})], cursors)
    files = list(tmp_path.rglob("*.jsonl"))
    assert files
    assert '"stream_id":"1-0"' in files[0].read_text()
    assert cursors["stream:test"]["id"] == "1-0"
    assert writer.load()["stream:test"]["id"] == "1-0"
