"""Read-only operator console adapter. No order, credential or runtime mutation routes.

Standalone mode exposes an unattached/empty snapshot, not simulated trading results.
Attach to HealthServer explicitly with a long bearer token to read a running system.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import math
import re
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from aitos.config.tuning import load_tuning_settings
from aitos.console_readiness import assess_readiness
from aitos.projection_evidence import public_projection, unavailable
from aitos.forensics.data_flow_registry import get_data_flow_registry

TRADE_FIELDS = (
    "trade_id",
    "symbol",
    "side",
    "state",
    "entry_price",
    "quantity",
    "position_size_usd",
    "risk_amount_usd",
    "sl_price",
    "tp_price",
    "pnl",
    "pnl_percent",
    "entry_time",
    "exit_time",
    "exit_price",
    "exit_reason",
    "fees_paid_usd",
    "funding_pnl_usd",
    "strategy_id",
)


def public_trade(trade: Any) -> dict:
    source = trade.to_dict() if hasattr(trade, "to_dict") else trade
    return {key: source.get(key) for key in TRADE_FIELDS}


def finite_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [finite_json(item) for item in value]
    return value


def register_console_routes(
    app: web.Application,
    modules=(),
    *,
    state_store=None,
    token: str | None = None,
    runtime_mode: str = "unknown",
    projection_observer=None,
    account_observer=None,
    protection_observer=None,
    history_pages=None,
) -> None:
    modules = list(modules)
    if runtime_mode not in {"unknown", "paper", "live", "backtest"}:
        raise ValueError("invalid declared runtime mode")
    if (
        modules
        or state_store is not None
        or projection_observer is not None
        or account_observer is not None
        or protection_observer is not None
        or history_pages is not None
    ) and (not token or len(token) < 32):
        raise ValueError(
            "attached operator console requires a bearer token of at least 32 characters"
        )

    def authorize(request):
        if token and not hmac.compare_digest(
            request.headers.get("Authorization", "").encode("utf-8"),
            f"Bearer {token}".encode("utf-8"),
        ):
            raise web.HTTPUnauthorized(text="Console authorization required")

    async def snapshot(request):
        authorize(request)

        async def health(module):
            try:
                result = await asyncio.wait_for(module.health_check(), timeout=2)
                return {"name": module.module_id, "status": result.status.value}
            except Exception:
                return {"name": module.module_id, "status": "unavailable"}

        statuses = await asyncio.gather(*(health(module) for module in modules))
        lifecycle = next((m for m in modules if m.module_id == "trade-lifecycle"), None)
        config = load_tuning_settings().model_dump(mode="json")
        payload = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "connected": bool(modules),
            "source": "runtime" if modules else "unattached",
            "mode": runtime_mode,
            "controls": "read_only",
            "modules": statuses,
            "positions": (
                [public_trade(t) for t in lifecycle.get_open_trades()]
                if lifecycle
                else []
            ),
            "pipeline": (
                [s.to_dict() for s in get_data_flow_registry().pipeline_snapshot()]
                if modules
                else []
            ),
            "configuration": config,
            "history_available": state_store is not None,
            "notes": [
                "No mutation or credential routes are exposed.",
                "Stage percentiles are process-local samples, not exchange round-trip latency.",
            ],
        }
        projection = unavailable()
        if projection_observer is not None:
            try:
                projection = public_projection(projection_observer.public_status())
            except Exception:
                projection = unavailable(
                    "operational_error",
                    "failed",
                    datetime.now(timezone.utc).isoformat(),
                )
        payload["projection"] = projection
        payload["readiness"] = assess_readiness(payload)
        return web.json_response(
            finite_json(payload), headers={"Cache-Control": "no-store"}
        )

    async def history(request):
        authorize(request)
        try:
            limit = int(request.query.get("limit", "50"))
            offset = int(request.query.get("offset", "0"))
            symbol = request.query.get("symbol", "").upper()
            if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
                raise ValueError()
            if symbol and not re.fullmatch(r"[A-Z0-9]{2,24}", symbol):
                raise ValueError()
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid bounded history query") from None
        if state_store is None:
            return web.json_response(
                {"source": "unavailable", "rows": [], "available": False}, status=503
            )
        try:
            rows = await asyncio.wait_for(
                state_store.query_closed_history(
                    limit=limit, offset=offset, symbol=symbol
                ),
                5,
            )
        except Exception:
            return web.json_response(
                {
                    "source": "clickhouse",
                    "available": False,
                    "rows": [],
                    "error": "History store unavailable",
                },
                status=503,
            )
        return web.json_response(
            finite_json(
                {
                    "source": "clickhouse",
                    "available": True,
                    "rows": [public_trade(row) for row in rows],
                    "limit": limit,
                    "offset": offset,
                }
            ),
            headers={"Cache-Control": "no-store"},
        )

    async def performance(request):
        authorize(request)
        if request.query:
            raise web.HTTPBadRequest(
                text="Performance endpoint uses a fixed bounded sample"
            )
        try:
            if state_store is None:
                raise RuntimeError()
            report = await asyncio.wait_for(
                state_store.query_closed_performance(limit=1000), 5
            )
        except Exception:
            return web.json_response(
                {
                    "available": False,
                    "source": "unavailable",
                    "error": "Performance evidence unavailable",
                },
                status=503,
                headers={"Cache-Control": "no-store"},
            )
        report["generated_at"] = datetime.now(timezone.utc).isoformat()
        return web.json_response(
            finite_json(report), headers={"Cache-Control": "no-store"}
        )

    async def research_plan(request):
        authorize(request)
        from aitos.learning.evaluation_plan import (
            EvaluationPlanRequest,
            build_evaluation_plan,
        )

        allowed = {
            "start",
            "end",
            "train_days",
            "test_days",
            "holdout_days",
            "label_horizon_days",
            "embargo_days",
            "dataset_sha256",
        }
        try:
            if set(request.query) - allowed or any(
                len(request.query.getall(key)) != 1 for key in request.query
            ):
                raise ValueError()
            values = dict(request.query)
            for key in allowed - {"start", "end", "dataset_sha256"}:
                if key in values:
                    if not re.fullmatch(r"[0-9]{1,4}", values[key]):
                        raise ValueError()
                    values[key] = int(values[key])
            report = build_evaluation_plan(EvaluationPlanRequest(**values))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid bounded research plan") from None
        return web.json_response(report, headers={"Cache-Control": "no-store"})

    async def history_cursor(request):
        authorize(request)
        headers = {"Cache-Control": "no-store"}
        try:
            if set(request.query) - {"limit", "cursor"} or any(
                len(request.query.getall(key)) != 1 for key in request.query
            ):
                raise ValueError()
            limit = int(request.query.get("limit", "100"))
            cursor = request.query.get("cursor")
            if not 1 <= limit <= 100 or (cursor is not None and len(cursor) > 256):
                raise ValueError()
        except ValueError:
            raise web.HTTPBadRequest(
                text="Invalid history continuation query"
            ) from None
        if history_pages is None:
            return web.json_response(
                {"available": False, "error": "History materializer not attached"},
                status=503,
                headers=headers,
            )
        try:
            result = await asyncio.wait_for(
                history_pages.page(limit=limit, cursor=cursor), timeout=8
            )
        except ValueError:
            raise web.HTTPBadRequest(
                text="Invalid, expired or changed history continuation"
            ) from None
        except Exception:
            return web.json_response(
                {"available": False, "error": "History materialization unavailable"},
                status=503,
                headers=headers,
            )
        return web.json_response(result, headers=headers)

    async def protection_observation(request):
        authorize(request)
        if request.query:
            raise web.HTTPBadRequest(
                text="Protection query parameters are not supported"
            )
        headers = {"Cache-Control": "no-store"}
        if protection_observer is None:
            return web.json_response(
                {"available": False, "error": "Protection observer not attached"},
                status=503,
                headers=headers,
            )
        try:
            result = await asyncio.wait_for(protection_observer.observe(), timeout=10)
        except Exception:
            return web.json_response(
                {"available": False, "error": "Protection evidence unavailable"},
                status=503,
                headers=headers,
            )
        return web.json_response(result, headers=headers)

    async def account_timeline(request):
        authorize(request)
        headers = {"Cache-Control": "no-store"}
        try:
            if set(request.query) - {"limit", "cursor"} or any(
                len(request.query.getall(key)) != 1 for key in request.query
            ):
                raise ValueError()
            limit = int(request.query.get("limit", "50"))
            cursor = request.query.get("cursor")
            if not 1 <= limit <= 100 or (cursor is not None and len(cursor) > 512):
                raise ValueError()
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid timeline query") from None
        timeline = getattr(account_observer, "timeline", None)
        if timeline is None:
            return web.json_response(
                {"available": False, "error": "Account timeline not attached"},
                status=503,
                headers=headers,
            )
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(timeline.page, limit=limit, cursor=cursor), timeout=5
            )
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid timeline cursor") from None
        except Exception:
            return web.json_response(
                {"available": False, "error": "Account timeline unavailable"},
                status=503,
                headers=headers,
            )
        return web.json_response(result, headers=headers)

    async def account_observation(request):
        authorize(request)
        if request.query:
            raise web.HTTPBadRequest(text="Account query parameters are not supported")
        headers = {"Cache-Control": "no-store"}
        if account_observer is None:
            return web.json_response(
                {"available": False, "error": "Account provider not attached"},
                status=503,
                headers=headers,
            )
        try:
            # Provider explicitly injected by the runtime owner, never selected
            # by browser parameters. No credentials or executor passed here.
            result = await asyncio.wait_for(account_observer.observe(), timeout=12)
        except Exception:
            return web.json_response(
                {"available": False, "error": "Account observation unavailable"},
                status=503,
                headers=headers,
            )
        return web.json_response(result, headers=headers)

    async def capabilities(request):
        from aitos.console_capabilities import describe_capabilities

        authorize(request)
        if request.query:
            raise web.HTTPBadRequest(
                text="Capability query parameters are not supported"
            )
        return web.json_response(
            describe_capabilities(
                modules,
                state_store,
                projection_observer,
                account_observer,
                history_pages,
            ),
            headers={"Cache-Control": "no-store"},
        )

    app.router.add_get("/console/v1/history/cursor", history_cursor)
    app.router.add_get("/console/v1/protection", protection_observation)
    app.router.add_get("/console/v1/account/timeline", account_timeline)
    app.router.add_get("/console/v1/account", account_observation)
    app.router.add_get("/console/v1/capabilities", capabilities)
    app.router.add_get("/console/v1/research/plan", research_plan)
    app.router.add_get("/console/v1/performance", performance)
    app.router.add_get("/console/v1/snapshot", snapshot)
    app.router.add_get("/console/v1/history", history)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    app = web.Application()
    register_console_routes(app)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
