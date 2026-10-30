from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

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
        self.warnings: list[tuple[str, object]] = []

    def error(self, message: str, error: object) -> None:
        self.errors.append((message, error))

    def warning(self, message: str, error: object) -> None:
        self.warnings.append((message, error))


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


class FakeRepository:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.initialized = False
        self.shutdown_count = 0

    async def initialize(self, config) -> None:
        self.initialized = True

    async def shutdown(self) -> None:
        self.shutdown_count += 1


@pytest.mark.asyncio
async def test_connect_clickhouse_repositories_initializes_both(monkeypatch) -> None:
    from aitos.data import repository as data_repository
    from aitos.journal import repository as journal_repository

    market_repo = FakeRepository()
    journal_repo = FakeRepository()
    market_kwargs = {}
    journal_kwargs = {}
    monkeypatch.setattr(
        data_repository,
        "MarketDataRepository",
        lambda **kwargs: market_kwargs.update(kwargs) or market_repo,
    )
    monkeypatch.setattr(
        journal_repository,
        "JournalRepository",
        lambda **kwargs: journal_kwargs.update(kwargs) or journal_repo,
    )
    retry_calls = []

    async def run_once(fn, **kwargs):
        retry_calls.append(kwargs)
        return await fn()

    monkeypatch.setattr(runtime_connections, "retry_with_backoff", run_once)
    settings = SimpleNamespace(
        clickhouse=SimpleNamespace(
            host="localhost",
            port=8123,
            user="aitos",
            password="test",
            database="aitos_test",
        )
    )

    result = await runtime_connections.connect_clickhouse_repositories(
        settings, logger=FakeLogger()
    )

    assert result == (market_repo, journal_repo)
    assert market_repo.initialized
    assert journal_repo.initialized
    expected_connection = {
        "host": "localhost",
        "port": 8123,
        "username": "aitos",
        "password": "test",
        "database": "aitos_test",
    }
    assert market_kwargs == expected_connection
    assert journal_kwargs == expected_connection
    assert [call["operation_name"] for call in retry_calls] == [
        "ClickHouse market repository initialization",
        "ClickHouse journal repository initialization",
    ]
    assert all(call["max_attempts"] == 5 for call in retry_calls)


@pytest.mark.asyncio
async def test_connect_clickhouse_repositories_shuts_down_on_retry_exhaustion(
    monkeypatch,
) -> None:
    from aitos.data import repository as data_repository
    from aitos.journal import repository as journal_repository

    market_repo = FakeRepository()
    journal_repo = FakeRepository()
    monkeypatch.setattr(
        data_repository, "MarketDataRepository", lambda **kwargs: market_repo
    )
    monkeypatch.setattr(
        journal_repository, "JournalRepository", lambda **kwargs: journal_repo
    )
    failure = RetryExhaustedError(5, ConnectionError("ClickHouse unavailable"))

    async def fail_retry(*args, **kwargs):
        raise failure

    monkeypatch.setattr(runtime_connections, "retry_with_backoff", fail_retry)
    logger = FakeLogger()
    settings = SimpleNamespace(
        clickhouse=SimpleNamespace(
            host="localhost",
            port=8123,
            user="aitos",
            password="test",
            database="aitos_test",
        )
    )

    with pytest.raises(SystemExit) as exc_info:
        await runtime_connections.connect_clickhouse_repositories(
            settings, logger=logger
        )

    assert exc_info.value.code == 1
    assert market_repo.shutdown_count == 1
    assert journal_repo.shutdown_count == 1
    assert logger.errors == [
        ("ClickHouse persistence unavailable after retries: %s", failure)
    ]


@pytest.mark.asyncio
async def test_try_connect_neo4j_returns_connected_driver(monkeypatch) -> None:
    class FakeDriver:
        async def verify_connectivity(self) -> None:
            return None

        async def close(self) -> None:
            raise AssertionError("connected driver must not be closed")

    driver = FakeDriver()
    neo4j_module = ModuleType("neo4j")
    neo4j_module.AsyncGraphDatabase = SimpleNamespace(driver=lambda uri, auth: driver)
    monkeypatch.setitem(sys.modules, "neo4j", neo4j_module)
    settings = SimpleNamespace(
        neo4j=SimpleNamespace(
            uri="bolt://localhost:7687", user="neo4j", password="test"
        )
    )

    result = await runtime_connections.try_connect_neo4j(settings, logger=FakeLogger())

    assert result is driver


@pytest.mark.asyncio
async def test_try_connect_neo4j_closes_driver_when_unavailable(monkeypatch) -> None:
    class FakeDriver:
        def __init__(self) -> None:
            self.close_count = 0

        async def verify_connectivity(self) -> None:
            raise ConnectionError("neo4j unavailable")

        async def close(self) -> None:
            self.close_count += 1

    driver = FakeDriver()
    neo4j_module = ModuleType("neo4j")
    neo4j_module.AsyncGraphDatabase = SimpleNamespace(driver=lambda uri, auth: driver)
    monkeypatch.setitem(sys.modules, "neo4j", neo4j_module)
    logger = FakeLogger()
    settings = SimpleNamespace(
        neo4j=SimpleNamespace(
            uri="bolt://localhost:7687", user="neo4j", password="test"
        )
    )

    result = await runtime_connections.try_connect_neo4j(settings, logger=logger)

    assert result is None
    assert driver.close_count == 1
    assert len(logger.warnings) == 1
    assert logger.warnings[0][0] == "Neo4j unavailable: %s"
    assert isinstance(logger.warnings[0][1], ConnectionError)
