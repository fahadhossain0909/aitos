"""HealthServer with JSON health, Prometheus metrics, and optional alerts."""

from __future__ import annotations

import os
import time
from typing import Iterable, List, Optional

import aiohttp
from aiohttp import web

from aitos.core.contracts import AITOSModule, ModuleStatus
from aitos.logging_setup import get_logger

logger = get_logger("aitos.health_server")


class HealthServer:
    def __init__(
        self,
        modules: Iterable[AITOSModule],
        host: str = "127.0.0.1",
        port: int = 8090,
        alert_webhook_url: Optional[str] = None,
        alert_cooldown_seconds: float = 900.0,
    ) -> None:
        self._modules: List[AITOSModule] = list(modules)
        self._host = host
        self._port = port
        self._runner: Optional[web.AppRunner] = None
        self._alert_webhook_url = alert_webhook_url or os.getenv("AITOS_ALERT_WEBHOOK_URL")
        self._alert_cooldown_seconds = alert_cooldown_seconds
        self._last_alert_at: dict[str, float] = {}

    async def start(self) -> None:
        app = self._build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info(
            "health server listening",
            extra={"aitos_extra": {"host": self._host, "port": self._port}},
        )

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/metrics", self._handle_metrics)
        return app

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_health(self, request: web.Request) -> web.Response:
        results = []
        overall_healthy = True
        degraded_modules = []
        for module in self._modules:
            health = await module.health_check()
            if health.status != ModuleStatus.HEALTHY:
                overall_healthy = False
                degraded_modules.append(health.module_id)
            results.append(
                {
                    "module_id": health.module_id,
                    "status": health.status.value,
                    "latency_ms": health.latency_ms,
                    "last_event_time": health.last_event_time,
                    "details": health.details,
                }
            )

        if degraded_modules:
            await self._maybe_alert(degraded_modules)

        payload = {
            "status": "healthy" if overall_healthy else "degraded",
            "modules": results,
        }
        return web.json_response(payload, status=200 if overall_healthy else 503)

    async def _maybe_alert(self, degraded_modules: List[str]) -> None:
        if not self._alert_webhook_url:
            return

        now = time.monotonic()
        key = ",".join(sorted(degraded_modules))
        if now - self._last_alert_at.get(key, 0.0) < self._alert_cooldown_seconds:
            return

        message = "AITOS health alert: degraded modules: " + ", ".join(degraded_modules)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._alert_webhook_url,
                    json={"text": message},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    if response.status >= 400:
                        logger.warning(
                            "health alert webhook returned HTTP %s", response.status
                        )
                        return
        except Exception as exc:
            logger.warning("health alert webhook failed: %s", exc)
            return

        self._last_alert_at[key] = now

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        lines = [
            "# HELP aitos_module_healthy Whether a module reports healthy (1) or not (0)",
            "# TYPE aitos_module_healthy gauge",
        ]
        for module in self._modules:
            health = await module.health_check()
            value = 1 if health.status == ModuleStatus.HEALTHY else 0
            lines.append(f'aitos_module_healthy{{module="{health.module_id}"}} {value}')

            for key, val in health.details.items():
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    metric_name = f"aitos_{health.module_id.replace('-', '_')}_{key}"
                    lines.append(f"# TYPE {metric_name} gauge")
                    lines.append(f"{metric_name} {val}")

        return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")
