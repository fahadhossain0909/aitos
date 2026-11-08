"""Capability-aware storage backend selection.

ClickHouse remains the analytical system of record. A fallback is selected
only when a concrete adapter advertises every required capability and its
connectivity probe succeeds; a healthy database alone is never sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Mapping


class StorageBackend(str, Enum):
    CLICKHOUSE = "clickhouse"
    POSTGRES = "postgres"


class StorageBackendError(RuntimeError):
    """No configured backend can safely satisfy the requested workload."""


@dataclass(frozen=True)
class BackendAdapter:
    backend: StorageBackend
    capabilities: frozenset[str]
    probe: Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class BackendSelection:
    backend: StorageBackend
    fallback_used: bool
    capabilities: frozenset[str]


async def select_storage_backend(
    *,
    adapters: Mapping[StorageBackend, BackendAdapter],
    required_capabilities: frozenset[str],
    primary: StorageBackend = StorageBackend.CLICKHOUSE,
    fallback: StorageBackend = StorageBackend.POSTGRES,
    allow_fallback: bool = True,
) -> BackendSelection:
    """Select primary first; use fallback only with full capability parity.

    Probe exceptions are deliberately not swallowed: they are included in the
    final error while the selector never silently degrades required features.
    """
    order = [primary]
    if allow_fallback and fallback != primary:
        order.append(fallback)
    failures: list[str] = []
    for index, backend in enumerate(order):
        adapter = adapters.get(backend)
        if adapter is None:
            failures.append(f"{backend.value}: adapter is not registered")
            continue
        missing = required_capabilities - adapter.capabilities
        if missing:
            failures.append(
                f"{backend.value}: missing capabilities {', '.join(sorted(missing))}"
            )
            continue
        try:
            await adapter.probe()
        except Exception as exc:  # connectivity errors must trigger safe fallback
            failures.append(f"{backend.value}: probe failed ({type(exc).__name__})")
            continue
        return BackendSelection(
            backend=backend,
            fallback_used=index > 0,
            capabilities=adapter.capabilities,
        )
    requested = ", ".join(sorted(required_capabilities)) or "(none)"
    raise StorageBackendError(
        f"No safe storage backend available for capabilities {requested}. "
        + "; ".join(failures)
    )
