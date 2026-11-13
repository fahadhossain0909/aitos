"""Environment-configured WIATS worker entrypoint.

Run with `python -m aitos.world_intelligence.runner`. Secrets are read from
environment variables; no credentials are committed or written to event data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections.abc import Mapping
from typing import Any

from aitos.eventbus.factory import create_event_bus
from aitos.world_intelligence.adapters import IntelligenceSourceAdapter
from aitos.world_intelligence.eventbus_adapter import EventBusSink
from aitos.world_intelligence.licensed_api import LicensedJSONFeedAdapter
from aitos.world_intelligence.providers import (
    FREDSeriesAdapter,
    GDELTDocAdapter,
    SECEdgarSubmissionsAdapter,
)
from aitos.world_intelligence.rss import RSSFeedAdapter
from aitos.world_intelligence.storage import ClickHouseIntelligenceStore
from aitos.world_intelligence.worker import IntelligenceIngestionWorker

logger = logging.getLogger("aitos.world_intelligence.runner")


def build_source_adapters(
    source_configs: list[Mapping[str, Any]],
    *,
    environ: Mapping[str, str] | None = None,
) -> list[IntelligenceSourceAdapter]:
    """Construct only explicitly configured sources; secrets are env references."""
    env = environ or os.environ
    adapters: list[IntelligenceSourceAdapter] = []
    for config in source_configs:
        kind = str(config.get("type", "")).strip().lower()
        user_agent = str(
            config.get("user_agent")
            or env.get("AITOS_CONTACT_USER_AGENT", "AITOS-WIATS/1.0 operator-configured")
        )
        common = {
            "source_id": str(config.get("source_id", "")).strip(),
            "user_agent": user_agent,
            "timeout_seconds": float(config.get("timeout_seconds", 10.0)),
            "min_interval_seconds": float(config.get("min_interval_seconds", 1.0)),
        }
        if kind == "rss":
            adapters.append(
                RSSFeedAdapter(
                    source_id=common["source_id"],
                    feed_url=str(config["feed_url"]),
                    allowed_hosts=set(config["allowed_hosts"]),
                    reliability=float(config.get("reliability", 0.5)),
                    source_kind=str(config.get("source_kind", "news_feed")),
                    is_primary=bool(config.get("is_primary", False)),
                    max_items=int(config.get("max_items", 100)),
                    timeout_seconds=common["timeout_seconds"],
                )
            )
        elif kind == "gdelt":
            adapters.append(
                GDELTDocAdapter(
                    query=str(config["query"]),
                    source_id=common["source_id"] or "gdelt-doc",
                    lookback_minutes=int(config.get("lookback_minutes", 15)),
                    max_records=int(config.get("max_records", 100)),
                    user_agent=user_agent,
                    timeout_seconds=common["timeout_seconds"],
                    min_interval_seconds=common["min_interval_seconds"],
                )
            )
        elif kind == "sec":
            contact = str(
                config.get("contact_user_agent")
                or env.get("AITOS_SEC_USER_AGENT")
                or env.get("AITOS_CONTACT_USER_AGENT", "")
            )
            adapters.append(
                SECEdgarSubmissionsAdapter(
                    cik=str(config["cik"]),
                    contact_user_agent=contact,
                    source_id=common["source_id"] or None,
                    max_filings=int(config.get("max_filings", 100)),
                    timeout_seconds=common["timeout_seconds"],
                    min_interval_seconds=common["min_interval_seconds"],
                )
            )
        elif kind == "licensed_json":
            header_env = config.get("header_env", {})
            if not isinstance(header_env, dict):
                raise ValueError("licensed JSON header_env must be a JSON object")
            headers = {}
            for header_name, env_name in header_env.items():
                secret = env.get(str(env_name), "")
                if not secret:
                    raise ValueError(
                        f"licensed API header environment variable {env_name!r} is missing"
                    )
                headers[str(header_name)] = secret
            adapters.append(
                LicensedJSONFeedAdapter(
                    source_id=common["source_id"],
                    endpoint=str(config["endpoint"]),
                    allowed_hosts=set(config["allowed_hosts"]),
                    records_path=str(config["records_path"]),
                    field_map=config["field_map"],
                    query_params=config.get("query_params", {}),
                    headers=headers,
                    cursor_param=config.get("cursor_param"),
                    source_kind=str(config.get("source_kind", "licensed_news_or_social")),
                    reliability=float(config.get("reliability", 0.4)),
                    max_records=int(config.get("max_records", 100)),
                    max_response_bytes=int(config.get("max_response_bytes", 2_000_000)),
                    timeout_seconds=common["timeout_seconds"],
                    min_interval_seconds=common["min_interval_seconds"],
                )
            )
        elif kind == "fred":
            key_env = str(config.get("api_key_env", "FRED_API_KEY"))
            api_key = env.get(key_env, "")
            if not api_key:
                raise ValueError(f"FRED API key environment variable {key_env!r} is missing")
            adapters.append(
                FREDSeriesAdapter(
                    series_id=str(config["series_id"]),
                    api_key=api_key,
                    start_date=str(config["start_date"]),
                    source_id=common["source_id"] or None,
                    max_observations=int(config.get("max_observations", 1000)),
                    user_agent=user_agent,
                    timeout_seconds=common["timeout_seconds"],
                    min_interval_seconds=common["min_interval_seconds"],
                )
            )
        else:
            raise ValueError(f"unsupported WIATS source type: {kind!r}")
    if not adapters:
        raise ValueError("configure at least one WIATS source")
    if len({adapter.source_id for adapter in adapters}) != len(adapters):
        raise ValueError("WIATS source_id values must be unique")
    return adapters


def _load_json_env(name: str, default: Any) -> Any:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain valid JSON") from exc


async def _run() -> None:
    import clickhouse_connect
    from redis.asyncio import Redis

    source_configs = _load_json_env("AITOS_WORLD_INTELLIGENCE_SOURCES", [])
    if not isinstance(source_configs, list):
        raise ValueError("AITOS_WORLD_INTELLIGENCE_SOURCES must be a JSON array")
    publisher_groups = _load_json_env("AITOS_WORLD_INTELLIGENCE_PUBLISHER_GROUPS", {})
    if not isinstance(publisher_groups, dict):
        raise ValueError("AITOS_WORLD_INTELLIGENCE_PUBLISHER_GROUPS must be a JSON object")
    adapters = build_source_adapters(source_configs)

    clickhouse = await clickhouse_connect.get_async_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos"),
        secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
    )
    redis_client = Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=False,
    )
    event_bus = create_event_bus(
        {"backend": "redis", "runtime_mode": "production", "module_id": "wiats-ingestion"},
        redis_client=redis_client,
    )
    worker = IntelligenceIngestionWorker(
        adapters,
        ClickHouseIntelligenceStore(
            clickhouse, database=os.getenv("CLICKHOUSE_DATABASE", "aitos")
        ),
        EventBusSink(event_bus),
        poll_interval_seconds=float(
            os.getenv("AITOS_WORLD_INTELLIGENCE_POLL_SECONDS", "30")
        ),
        publisher_groups=publisher_groups,
    )
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, worker.stop)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await event_bus.initialize({})
        await worker.run_forever()
    finally:
        worker.stop()
        await event_bus.shutdown()
        await redis_client.aclose()
        await clickhouse.close()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("AITOS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
