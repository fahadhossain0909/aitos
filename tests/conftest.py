"""Shared test fixtures.

Tests run against ``fakeredis`` by default so the suite is fast and doesn't
require Docker. To run against real infra instead:

    REDIS_HOST=localhost pytest --real-redis

(the ``--real-redis`` flag is illustrative — wire it up if/when you want a
CI job against the actual docker-compose stack.)
"""

from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
from pathlib import Path

# ``aitos.app`` currently creates its online-model directories at import time
# using a developer-specific ``/home/fahad`` path. Keep that legacy path from
# breaking CI/test collection by redirecting only those exact model-directory
# creations to a writable temporary location. This is deliberately test-only;
# production path handling remains unchanged.
_TEST_MODEL_ROOT = Path(tempfile.mkdtemp(prefix="aitos-models-"))
_ORIGINAL_MAKEDIRS = os.makedirs
_MODEL_PATH_PREFIX = "/home/fahad/aitos/models/"


def _test_makedirs(path, mode=0o777, exist_ok=False):
    raw_path = os.fspath(path)
    if raw_path.startswith(_MODEL_PATH_PREFIX):
        path = _TEST_MODEL_ROOT / raw_path[len(_MODEL_PATH_PREFIX) :]
    return _ORIGINAL_MAKEDIRS(path, mode=mode, exist_ok=exist_ok)


os.makedirs = _test_makedirs

import pytest
import pytest_asyncio
from fakeredis import aioredis as fake_aioredis

from aitos.eventbus.redis_bus import EventBus
from aitos.kernel.ai_kernel import AIKernel
from aitos.risk.risk_engine import RiskEngine


@pytest.fixture(scope="session", autouse=True)
def restore_test_makedirs():
    """Restore the process-global ``os.makedirs`` patch after the test run."""
    yield
    os.makedirs = _ORIGINAL_MAKEDIRS


@pytest_asyncio.fixture
async def fake_redis():
    client = fake_aioredis.FakeRedis(decode_responses=False)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def event_bus(fake_redis):
    bus = EventBus(redis_client=fake_redis)
    await bus.initialize({})
    yield bus
    await bus.shutdown(grace_period_seconds=1.0)


@pytest_asyncio.fixture
async def kernel(event_bus):
    k = AIKernel(event_bus=event_bus)
    await k.initialize({})
    yield k
    await k.shutdown(grace_period_seconds=1.0)


@pytest_asyncio.fixture
async def risk_engine(event_bus):
    engine = RiskEngine(event_bus=event_bus)
    await engine.initialize({})
    yield engine
    await engine.shutdown()


@pytest.fixture(autouse=True)
def async_task_watchdog(request):
    """Expose async lifecycle hangs instead of letting CI sit silently."""
    yield
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    if loop.is_closed():
        return
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    if not pending:
        return
    print(
        f"[async-watchdog] {request.node.nodeid}: {len(pending)} live task(s) remain after test",
        flush=True,
    )
    for task in pending:
        print(
            f"[async-watchdog] task={task.get_name()} coro={task.get_coro()!r}",
            flush=True,
        )
        for frame in task.get_stack(limit=8):
            info = inspect.getframeinfo(frame)
            context = (info.code_context or [""])[0].strip()
            print(
                f"[async-watchdog]   {info.filename}:{info.lineno} {context}",
                flush=True,
            )
