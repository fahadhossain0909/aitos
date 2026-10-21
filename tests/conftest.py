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

# Keep online-model artifacts outside the checkout during tests. The same
# environment override is used by local development and CI path resolution.
_TEST_MODEL_ROOT = Path(tempfile.mkdtemp(prefix="aitos-models-"))
os.environ["MODEL_DATA_DIR"] = str(_TEST_MODEL_ROOT)

import pytest
import pytest_asyncio
from fakeredis import aioredis as fake_aioredis

from aitos.eventbus.redis_bus import EventBus
from aitos.kernel.ai_kernel import AIKernel
from aitos.risk.risk_engine import RiskEngine


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


@pytest_asyncio.fixture(autouse=True)
async def _cancel_stray_tasks():
    """Cancel any tasks a test left running so they cannot leak into the
    next test on the shared event loop."""
    yield
    loop = asyncio.get_running_loop()
    tasks = [
        t
        for t in asyncio.all_tasks(loop)
        if not t.done() and t is not asyncio.current_task(loop)
    ]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    # Also close any remaining Redis connections to avoid resource leaks
    try:
        for task in tasks:
            coro = task.get_coro()
            if coro and hasattr(coro, "__self__"):
                obj = coro.__self__
                if hasattr(obj, "aclose"):
                    try:
                        await obj.aclose()
                    except Exception:
                        pass
    except Exception:
        pass


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
