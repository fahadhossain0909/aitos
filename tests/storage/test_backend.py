from __future__ import annotations

import pytest

from aitos.storage.backend import (
    BackendAdapter,
    StorageBackend,
    StorageBackendError,
    select_storage_backend,
)


@pytest.mark.asyncio
async def test_clickhouse_is_preferred_when_both_backends_are_healthy():
    calls = []

    async def probe(name):
        calls.append(name)

    adapters = {
        StorageBackend.CLICKHOUSE: BackendAdapter(
            StorageBackend.CLICKHOUSE, frozenset({"analytics", "journal"}),
            lambda: probe("clickhouse"),
        ),
        StorageBackend.POSTGRES: BackendAdapter(
            StorageBackend.POSTGRES, frozenset({"analytics", "journal"}),
            lambda: probe("postgres"),
        ),
    }
    result = await select_storage_backend(
        adapters=adapters, required_capabilities=frozenset({"analytics", "journal"})
    )
    assert result.backend is StorageBackend.CLICKHOUSE
    assert result.fallback_used is False
    assert calls == ["clickhouse"]


@pytest.mark.asyncio
async def test_postgres_fallback_only_after_primary_probe_failure():
    calls = []

    async def fail():
        calls.append("clickhouse")
        raise ConnectionError("offline")

    async def succeed():
        calls.append("postgres")

    adapters = {
        StorageBackend.CLICKHOUSE: BackendAdapter(
            StorageBackend.CLICKHOUSE, frozenset({"journal"}), fail
        ),
        StorageBackend.POSTGRES: BackendAdapter(
            StorageBackend.POSTGRES, frozenset({"journal"}), succeed
        ),
    }
    result = await select_storage_backend(
        adapters=adapters, required_capabilities=frozenset({"journal"})
    )
    assert result.backend is StorageBackend.POSTGRES
    assert result.fallback_used is True
    assert calls == ["clickhouse", "postgres"]


@pytest.mark.asyncio
async def test_healthy_postgres_is_not_selected_without_capability_parity():
    async def succeed():
        return None

    async def fail():
        raise ConnectionError("offline")

    adapters = {
        StorageBackend.CLICKHOUSE: BackendAdapter(
            StorageBackend.CLICKHOUSE, frozenset({"analytics", "journal"}), fail
        ),
        StorageBackend.POSTGRES: BackendAdapter(
            StorageBackend.POSTGRES, frozenset({"journal"}), succeed
        ),
    }
    with pytest.raises(StorageBackendError, match="missing capabilities analytics"):
        await select_storage_backend(
            adapters=adapters,
            required_capabilities=frozenset({"analytics", "journal"}),
        )


@pytest.mark.asyncio
async def test_fallback_can_be_disabled():
    async def fail():
        raise ConnectionError("offline")

    adapters = {
        StorageBackend.CLICKHOUSE: BackendAdapter(
            StorageBackend.CLICKHOUSE, frozenset({"journal"}), fail
        ),
        StorageBackend.POSTGRES: BackendAdapter(
            StorageBackend.POSTGRES, frozenset({"journal"}), fail
        ),
    }
    with pytest.raises(StorageBackendError):
        await select_storage_backend(
            adapters=adapters,
            required_capabilities=frozenset({"journal"}),
            allow_fallback=False,
        )
