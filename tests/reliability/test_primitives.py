from __future__ import annotations

import asyncio
import time

import pytest

from aitos.reliability import (
    AppendOnlyJournal,
    BackoffPolicy,
    Bulkhead,
    CircuitBreaker,
    CircuitOpenError,
    RetryExhaustedError,
    retry_async,
)


@pytest.mark.asyncio
async def test_retry_uses_bounded_exponential_backoff() -> None:
    calls = 0
    delays: list[float] = []

    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("temporary")
        return "ok"

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    result = await retry_async(
        op,
        policy=BackoffPolicy(attempts=3, initial_delay=0.1, multiplier=2, jitter=0),
        sleep=fake_sleep,
    )
    assert result == "ok"
    assert calls == 3
    assert delays == [0.1, 0.2]


@pytest.mark.asyncio
async def test_retry_exhaustion_preserves_cause() -> None:
    async def op() -> None:
        raise TimeoutError("downstream")

    with pytest.raises(RetryExhaustedError) as exc:
        await retry_async(op, policy=BackoffPolicy(attempts=2, initial_delay=0), sleep=asyncio.sleep)
    assert isinstance(exc.value.__cause__, TimeoutError)


@pytest.mark.asyncio
async def test_circuit_breaker_opens_and_recovers() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=5)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
    assert breaker.allow(now=time.monotonic()) is False
    assert breaker.allow(now=(breaker._opened_at or time.monotonic()) + 5) is True
    await breaker.call(_ok)
    assert breaker.state.value == "closed"


async def _fail() -> None:
    raise RuntimeError("failure")


async def _ok() -> str:
    return "ok"


@pytest.mark.asyncio
async def test_open_circuit_rejects_without_calling_operation() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30)
    breaker.failure()
    called = False

    async def op() -> None:
        nonlocal called
        called = True

    with pytest.raises(CircuitOpenError):
        await breaker.call(op)
    assert called is False


@pytest.mark.asyncio
async def test_bulkhead_bounds_concurrency() -> None:
    bulkhead = Bulkhead(2)
    active = 0
    peak = 0

    async def work() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    await asyncio.gather(*(bulkhead.run(work) for _ in range(8)))
    assert peak == 2


def test_append_only_journal_replays_and_detects_tampering(tmp_path) -> None:
    journal = AppendOnlyJournal(tmp_path / "events.jsonl")
    first = journal.append(
        "position.opened", {"symbol": "BTCUSDT", "qty": 1}, "2026-09-04T00:00:00Z"
    )
    second = journal.append(
        "position.closed", {"symbol": "BTCUSDT", "pnl": 12.5}, "2026-09-04T00:01:00Z"
    )
    records = journal.replay()
    assert [r.sequence for r in records] == [1, 2]
    assert first.record_hash == second.previous_hash

    text = journal.path.read_text(encoding="utf-8")
    journal.path.write_text(text.replace("12.5", "999.0"), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        journal.replay()
