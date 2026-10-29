from __future__ import annotations

from types import SimpleNamespace

import pytest

from aitos import runtime_connections
from aitos.resilience import RetryExhaustedError


class FakeRedis:
    def __init__(self) -> None:
        self.ping_count = 0

    async def ping(self) -> bool:
        self.ping_count += 1
        return True


class FakeLogger:
    def __init__(self) -> None:
        self.errors: list[tuple[str, object]] = []

    def error(self, message: str, error: object) -> None:
        self.errors.append((message, error))


@pytest.mark.asyncio
async def test_connect_redis_with_retry_returns_pinged_client(monkeypatch) -> None:
    client = FakeRedis()
    settings = SimpleNamespace(
        redis=SimpleNamespace(url="redis://localhost:6379/0", max_connections=16)
    )
    logger = FakeLogger()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_from_url(*args, **kwargs):
        calls.append((args, kwargs))
        return client

    monkeypatch.setattr(runtime_connections.Redis, "from_url", fake_from_url)

    result = await runtime_connections.connect_redis_with_retry(settings, logger=logger)

    assert result is client
    assert client.ping_count == 1
    assert calls == [
        (
            ("redis://localhost:6379/0",),
            {"max_connections": 16},
        )
    ]
    assert logger.errors == []


@pytest.mark.asyncio
async def test_connect_redis_with_retry_exits_after_retries_exhausted(
    monkeypatch,
) -> None:
    settings = SimpleNamespace(
        redis=SimpleNamespace(url="redis://localhost:6379/0", max_connections=16)
    )
    logger = FakeLogger()
    failure = RetryExhaustedError(5, ConnectionError("redis unavailable"))

    async def fail_retry(*args, **kwargs):
        raise failure

    monkeypatch.setattr(runtime_connections, "retry_with_backoff", fail_retry)

    with pytest.raises(SystemExit) as exc_info:
        await runtime_connections.connect_redis_with_retry(settings, logger=logger)

    assert exc_info.value.code == 1
    assert logger.errors == [("could not connect to Redis: %s", failure)]
