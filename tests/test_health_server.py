import pytest
from aiohttp.test_utils import TestClient, TestServer

from aitos.core.contracts import (AITOSModule, Event, EventResponse,
                                  HealthStatus, ModuleStatus)
from aitos.health_server import HealthServer


class FakeModule(AITOSModule):
    def __init__(self, module_id: str, status: ModuleStatus, details=None):
        self._id = module_id
        self._status = status
        self._details = details or {}

    @property
    def module_id(self) -> str:
        return self._id

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config):
        pass

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self._id,
            status=self._status,
            latency_ms=1.5,
            last_event_time=None,
            details=self._details,
        )

    async def shutdown(self, grace_period_seconds: float = 30.0):
        pass

    async def emit_events(self):
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event):
        return None


@pytest.mark.asyncio
async def test_health_endpoint_returns_200_when_all_healthy():
    modules = [
        FakeModule("mod-a", ModuleStatus.HEALTHY),
        FakeModule("mod-b", ModuleStatus.HEALTHY),
    ]
    server = HealthServer(modules)
    client = TestClient(TestServer(server._build_app()))
    await client.start_server()

    resp = await client.get("/health")
    body = await resp.json()

    assert resp.status == 200
    assert body["status"] == "healthy"
    assert len(body["modules"]) == 2

    await client.close()


@pytest.mark.asyncio
async def test_health_endpoint_returns_503_when_any_module_unhealthy():
    modules = [
        FakeModule("mod-a", ModuleStatus.HEALTHY),
        FakeModule("mod-b", ModuleStatus.UNHEALTHY),
    ]
    server = HealthServer(modules)
    client = TestClient(TestServer(server._build_app()))
    await client.start_server()

    resp = await client.get("/health")
    body = await resp.json()

    assert resp.status == 503
    assert body["status"] == "degraded"

    await client.close()


@pytest.mark.asyncio
async def test_health_endpoint_includes_module_details():
    modules = [FakeModule("mod-a", ModuleStatus.HEALTHY, details={"trades_open": 3})]
    server = HealthServer(modules)
    client = TestClient(TestServer(server._build_app()))
    await client.start_server()

    resp = await client.get("/health")
    body = await resp.json()

    assert body["modules"][0]["details"]["trades_open"] == 3

    await client.close()


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_prometheus_text_format():
    modules = [
        FakeModule(
            "mod-a", ModuleStatus.HEALTHY, details={"trades_open": 3, "errors": 0}
        )
    ]
    server = HealthServer(modules)
    client = TestClient(TestServer(server._build_app()))
    await client.start_server()

    resp = await client.get("/metrics")
    text = await resp.text()

    assert resp.content_type == "text/plain"
    assert 'aitos_module_healthy{module="mod-a"} 1' in text
    assert "aitos_mod_a_trades_open 3" in text

    await client.close()


@pytest.mark.asyncio
async def test_metrics_endpoint_marks_unhealthy_module_as_zero():
    modules = [FakeModule("mod-a", ModuleStatus.UNHEALTHY)]
    server = HealthServer(modules)
    client = TestClient(TestServer(server._build_app()))
    await client.start_server()

    resp = await client.get("/metrics")
    text = await resp.text()

    assert 'aitos_module_healthy{module="mod-a"} 0' in text

    await client.close()


@pytest.mark.asyncio
async def test_start_and_stop_binds_and_releases_a_real_port():
    import aiohttp

    modules = [FakeModule("mod-a", ModuleStatus.HEALTHY)]
    server = HealthServer(modules, host="127.0.0.1", port=18099)
    await server.start()

    async with aiohttp.ClientSession() as session:
        async with session.get("http://127.0.0.1:18099/health") as resp:
            assert resp.status == 200

    await server.stop()
