"""Failure ownership contracts; no real Redis, Neo4j or ClickHouse IO."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aitos import runtime_connections as connections
from aitos.resilience import RetryExhaustedError, retry_with_backoff


def settings():
    return SimpleNamespace(
        redis=SimpleNamespace(url="redis://unused:6379/0", max_connections=16),
        neo4j=SimpleNamespace(uri="bolt://unused:7687", user="test", password="test"),
        clickhouse=SimpleNamespace(
            host="unused", port=8123, user="test", password="test", database="test"
        ),
    )


async def immediate_retries(fn, **kwargs):
    return await retry_with_backoff(
        fn, max_attempts=3, base_delay_seconds=0, max_delay_seconds=0
    )


async def test_each_failed_redis_attempt_is_closed_before_next_attempt(monkeypatch):
    clients = [
        SimpleNamespace(ping=AsyncMock(side_effect=OSError()), aclose=AsyncMock())
        for _ in range(3)
    ]
    acquired = []

    def acquire(*args, **kwargs):
        for previous in acquired:
            previous.aclose.assert_awaited_once()
        result = clients[len(acquired)]
        acquired.append(result)
        return result

    monkeypatch.setattr(connections.Redis, "from_url", acquire)
    monkeypatch.setattr(connections, "retry_with_backoff", immediate_retries)
    with pytest.raises(SystemExit):
        await connections.connect_redis_with_retry(settings(), logger=Mock())
    assert len(acquired) == 3
    for client in clients:
        client.aclose.assert_awaited_once()


async def test_cancelled_redis_ping_closes_without_retry(monkeypatch):
    client = SimpleNamespace(
        ping=AsyncMock(side_effect=asyncio.CancelledError()), aclose=AsyncMock()
    )
    create = Mock(return_value=client)
    monkeypatch.setattr(connections.Redis, "from_url", create)
    with pytest.raises(asyncio.CancelledError):
        await connections.connect_redis_with_retry(settings(), logger=Mock())
    create.assert_called_once()
    client.aclose.assert_awaited_once()


async def test_successful_redis_client_remains_owned_by_caller(monkeypatch):
    first = SimpleNamespace(ping=AsyncMock(side_effect=OSError()), aclose=AsyncMock())
    second = SimpleNamespace(ping=AsyncMock(return_value=True), aclose=AsyncMock())
    monkeypatch.setattr(
        connections.Redis, "from_url", Mock(side_effect=[first, second])
    )
    monkeypatch.setattr(connections, "retry_with_backoff", immediate_retries)
    assert (
        await connections.connect_redis_with_retry(settings(), logger=Mock()) is second
    )
    first.aclose.assert_awaited_once()
    second.aclose.assert_not_awaited()


async def test_cancelled_neo4j_connectivity_closes_and_propagates(monkeypatch):
    import neo4j

    driver = SimpleNamespace(
        verify_connectivity=AsyncMock(side_effect=asyncio.CancelledError()),
        close=AsyncMock(),
    )
    monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", Mock(return_value=driver))
    with pytest.raises(asyncio.CancelledError):
        await connections.try_connect_neo4j(settings(), logger=Mock())
    driver.close.assert_awaited_once()


@pytest.mark.parametrize(
    "failure", [asyncio.CancelledError, RuntimeError, RetryExhaustedError]
)
@pytest.mark.parametrize("cleanup_fails", [True, False])
async def test_partial_clickhouse_startup_releases_both_repositories(
    monkeypatch, failure, cleanup_fails
):
    market = SimpleNamespace(initialize=AsyncMock(), shutdown=AsyncMock())
    original = (
        failure(3, OSError()) if failure is RetryExhaustedError else failure("injected")
    )
    journal = SimpleNamespace(
        initialize=AsyncMock(side_effect=original), shutdown=AsyncMock()
    )
    if cleanup_fails:
        market.shutdown.side_effect = OSError("cleanup unavailable")
        journal.shutdown.side_effect = OSError("cleanup unavailable")
    monkeypatch.setattr(
        "aitos.data.repository.MarketDataRepository", Mock(return_value=market)
    )
    monkeypatch.setattr(
        "aitos.journal.repository.JournalRepository", Mock(return_value=journal)
    )

    async def once(fn, **kwargs):
        return await fn()

    monkeypatch.setattr(connections, "retry_with_backoff", once)
    expected = SystemExit if failure is RetryExhaustedError else failure
    with pytest.raises(expected) as caught:
        await connections.connect_clickhouse_repositories(settings(), logger=Mock())
    if expected is not SystemExit:
        assert caught.value is original
    else:
        assert caught.value.code == 1
        assert caught.value.__cause__ is original
    market.shutdown.assert_awaited_once()
    journal.shutdown.assert_awaited_once()
