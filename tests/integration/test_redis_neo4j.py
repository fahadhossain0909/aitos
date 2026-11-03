"""Live dependency integration tests.

These tests intentionally exercise real Redis and Neo4j services rather than
fakes. They are skipped outside an integration environment and enabled by CI
when the service containers are available.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_redis_round_trip() -> None:
    redis = pytest.importorskip("redis.asyncio")
    client = redis.from_url(
        os.getenv("INTEGRATION_REDIS_URL", "redis://:ci-only-not-used@127.0.0.1:6379/0")
    )
    try:
        key = "aitos:integration:test"
        await client.set(key, "ok", ex=30)
        assert await client.get(key) == b"ok"
        await client.delete(key)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_neo4j_round_trip() -> None:
    neo4j = pytest.importorskip("neo4j")
    uri = os.getenv("INTEGRATION_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.getenv("INTEGRATION_NEO4J_USER", "neo4j")
    password = os.getenv("INTEGRATION_NEO4J_PASSWORD", "ci-neo4j-password")
    driver = neo4j.AsyncGraphDatabase.driver(uri, auth=(user, password))
    try:
        async with driver.session() as session:
            result = await session.run("RETURN 1 AS ok")
            record = await result.single()
            assert record["ok"] == 1
    finally:
        await driver.close()
