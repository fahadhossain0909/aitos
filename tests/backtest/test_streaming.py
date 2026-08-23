from datetime import datetime, timedelta

from aitos.backtest.streaming import (CheckpointManager, ChunkPlanner,
                                      JsonlChunkReader,
                                      StreamingBacktestEngine)


def test_chunk_planner_preserves_contiguous_windows():
    start = datetime(2024, 1, 1)
    end = datetime(2024, 1, 3)
    windows = list(ChunkPlanner(start, end, timedelta(days=1)).windows())
    assert windows == [(start, datetime(2024, 1, 2)), (datetime(2024, 1, 2), end)]


def test_streaming_engine_preserves_state_and_checkpoint(tmp_path):
    state = {"count": 0}

    def process(event):
        state["count"] += event

    checkpoint = CheckpointManager(tmp_path / "state.pkl")
    engine = StreamingBacktestEngine(
        process, lambda: dict(state), lambda value: state.update(value), checkpoint
    )
    chunks = [
        type("Chunk", (), {"index": 0, "events": (1, 2)})(),
        type("Chunk", (), {"index": 1, "events": (3,)})(),
    ]
    assert engine.run(chunks) == 3
    assert state["count"] == 6
    assert checkpoint.exists()


def test_jsonl_reader_chunks_events(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"timestamp":"2024-01-01T00:00:01","value":1}\n'
        '{"timestamp":"2024-01-01T00:00:02","value":2}\n'
        '{"timestamp":"2024-01-01T00:00:03","value":3}\n',
        encoding="utf-8",
    )
    planner = ChunkPlanner(
        datetime(2024, 1, 1), datetime(2024, 1, 1, 0, 0, 4), timedelta(seconds=2)
    )
    chunks = list(JsonlChunkReader(path).iter_chunks(planner))
    assert [len(chunk.events) for chunk in chunks] == [1, 2]
