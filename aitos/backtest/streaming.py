"""Memory-bounded, sequential backtest streaming primitives."""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence


@dataclass(frozen=True)
class BacktestChunk:
    """A bounded time window of events."""

    index: int
    start: datetime
    end: datetime
    events: Sequence[Any]


class ChunkPlanner:
    """Plan time windows without loading the dataset."""

    def __init__(self, start: datetime, end: datetime, chunk_size: timedelta):
        if end <= start:
            raise ValueError("end must be after start")
        if chunk_size.total_seconds() <= 0:
            raise ValueError("chunk_size must be positive")
        self.start = start
        self.end = end
        self.chunk_size = chunk_size

    def windows(self) -> Iterator[tuple[datetime, datetime]]:
        cursor = self.start
        while cursor < self.end:
            nxt = min(cursor + self.chunk_size, self.end)
            yield cursor, nxt
            cursor = nxt


class CheckpointManager:
    """Atomic checkpoint persistence for resumable backtests."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def save(self, state: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("wb") as handle:
            pickle.dump(state, handle, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(self.path)

    def load(self) -> Any:
        with self.path.open("rb") as handle:
            return pickle.load(handle)  # nosec B301 - checkpoint files are local application state

    def exists(self) -> bool:
        return self.path.exists()


class JsonlChunkReader:
    """Stream a timestamp-sorted JSONL event file in bounded time chunks."""

    def __init__(
        self, path: str | Path, parser: Callable[[dict[str, Any]], Any] | None = None
    ):
        self.path = Path(path)
        self.parser = parser or (lambda row: row)

    def iter_chunks(self, planner: ChunkPlanner) -> Iterator[BacktestChunk]:
        windows = iter(planner.windows())
        try:
            start, end = next(windows)
        except StopIteration:
            return
        bucket: list[Any] = []
        index = 0
        for line in self.path.open("r", encoding="utf-8"):
            if not line.strip():
                continue
            row = json.loads(line)
            ts = datetime.fromisoformat(row["timestamp"])
            while ts >= end:
                yield BacktestChunk(index, start, end, tuple(bucket))
                index += 1
                bucket = []
                try:
                    start, end = next(windows)
                except StopIteration:
                    return
            if start <= ts < end:
                bucket.append(self.parser(row))
        yield BacktestChunk(index, start, end, tuple(bucket))


class StreamingBacktestEngine:
    """Sequential chunk runner that preserves state between chunks."""

    def __init__(
        self,
        process_event: Callable[[Any], None],
        state_getter: Callable[[], Any],
        state_loader: Callable[[Any], None],
        checkpoint: CheckpointManager | None = None,
    ):
        self.process_event = process_event
        self.state_getter = state_getter
        self.state_loader = state_loader
        self.checkpoint = checkpoint

    def run(self, chunks: Iterable[BacktestChunk], resume: bool = False) -> int:
        start_index = 0
        if resume and self.checkpoint and self.checkpoint.exists():
            saved = self.checkpoint.load()
            start_index = int(saved["next_chunk"])
            self.state_loader(saved["state"])
        processed = 0
        for chunk in chunks:
            if chunk.index < start_index:
                continue
            for event in chunk.events:
                self.process_event(event)
                processed += 1
            if self.checkpoint:
                self.checkpoint.save(
                    {"next_chunk": chunk.index + 1, "state": self.state_getter()}
                )
        return processed
