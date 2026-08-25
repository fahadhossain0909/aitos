"""HealthServer — a small aiohttp-based HTTP server exposing ``/health``
(JSON, one entry per module) and ``/metrics`` (Prometheus text format),
so a process supervisor (systemd, Kubernetes, a load balancer) or a
monitoring stack has something real to poll instead of just watching logs.

Deliberately minimal: no auth, no TLS, no histogram/summary metric types
— just per-module status and a handful of counters already sitting in
each module's ``health_check()`` output. Bind it to localhost or behind a
reverse proxy in any real deployment.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from aiohttp import web

from aitos.core.contracts import AITOSModule, ModuleStatus
from aitos.logging_setup import get_logger

logger = get_logger("aitos.health_server")


class HealthServer:
    def __init__(
        self, modules: Iterable[AITOSModule], host: str = "127.0.0.1", port: int = 8090
    ) -> None:
        self._modules: List[AITOSModule] = list(modules)
        self._host = host
        self._port = port
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        logger.info(
            "starting health server",
            extra={"aitos_extra": {"host": self._host, "port": self._port, "module_count": len(self._modules)}},
        )
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
            logger.info("stopping health server")
            await self._runner.cleanup()
            self._runner = None

    async def _handle_health(self, request: web.Request) -> web.Response:
        results = []
        overall_healthy = True
        logger.info(
            "health check requested",
            extra={"aitos_extra": {"path": str(request.path), "module_count": len(self._modules)}},
        )
        for module in self._modules:
            module_id = getattr(module, "module_id", module.__class__.__name__)
            logger.info(
                "checking module health",
                extra={"aitos_extra": {"module": module_id}},
            )
            try:
                health = await module.health_check()
                healthy = health.status == ModuleStatus.HEALTHY
                if not healthy:
                    overall_healthy = False
                    logger.warning(
                        "module reported unhealthy",
                        extra={
                            "aitos_extra": {
                                "module": health.module_id,
                                "status": health.status.value,
                                "latency_ms": health.latency_ms,
                                "last_event_time": health.last_event_time,
                                "details": health.details,
                            }
                        },
                    )
                else:
                    logger.info(
                        "module reported healthy",
                        extra={
                            "aitos_extra": {
                                "module": health.module_id,
                                "latency_ms": health.latency_ms,
                                "details": health.details,
                            }
                        },
                    )
                results.append(
                    {
                        "module_id": health.module_id,
                        "status": health.status.value,
                        "latency_ms": health.latency_ms,
                        "last_event_time": health.last_event_time,
                        "details": health.details,
                    }
                )
            except Exception as exc:
                overall_healthy = False
                logger.exception(
                    "module health check raised exception",
                    extra={"aitos_extra": {"module": module_id, "error_type": type(exc).__name__}},
                )
                results.append(
                    {
                        "module_id": module_id,
                        "status": "unhealthy",
                        "latency_ms": None,
                        "last_event_time": None,
                        "details": {"error": str(exc), "error_type": type(exc).__name__},
                    }
                )
        payload = {
            "status": "healthy" if overall_healthy else "degraded",
            "modules": results,
        }
        status_code = 200 if overall_healthy else 503
        logger.info(
            "health check completed",
            extra={
                "aitos_extra": {
                    "status": payload["status"],
                    "http_status": status_code,
                    "unhealthy_modules": [
                        item["module_id"] for item in results if item["status"] != ModuleStatus.HEALTHY.value
                    ],
                }
            },
        )
        return web.json_response(payload, status=status_code)

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        logger.info(
            "metrics check requested",
            extra={"aitos_extra": {"path": str(request.path), "module_count": len(self._modules)}},
        )
        lines = [
            "# HELP aitos_module_healthy Whether a module reports healthy (1) or not (0)",
            "# TYPE aitos_module_healthy gauge",
        ]
        for module in self._modules:
            module_id = getattr(module, "module_id", module.__class__.__name__)
            try:
                health = await module.health_check()
                value = 1 if health.status == ModuleStatus.HEALTHY else 0
                logger.info(
                    "metrics module health sampled",
                    extra={
                        "aitos_extra": {
                            "module": health.module_id,
                            "healthy": value,
                            "details": health.details,
                        }
                    },
                )
            except Exception as exc:
                value = 0
                health = None
                logger.exception(
                    "metrics module health check raised exception",
                    extra={"aitos_extra": {"module": module_id, "error_type": type(exc).__name__}},
                )
            metric_module = health.module_id if health is not None else module_id
            lines.append(f'aitos_module_healthy{{module="{metric_module}"}} {value}')

            if health is not None:
                for key, val in health.details.items():
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        metric_name = f"aitos_{health.module_id.replace('-', '_')}_{key}"
                        lines.append(f"# TYPE {metric_name} gauge")
                        lines.append(f"{metric_name} {val}")

        logger.info("metrics check completed")
        return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")
