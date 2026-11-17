"""Opt-in standalone CFEI observer process. It never imports trading execution."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Any

import clickhouse_connect
import redis.asyncio as redis

from aitos.capital_intelligence.binance import BinanceFuturesCapitalAdapter
from aitos.capital_intelligence.binance_price import BinanceHistoricalPriceBook
from aitos.capital_intelligence.dex import EvmDexSwapAdapter
from aitos.capital_intelligence.enrichment import enrich_capital_event
from aitos.capital_intelligence.evm import EVMLogCapitalAdapter
from aitos.capital_intelligence.runtime import (
    CapitalIngestionWorker,
    RedisCheckpointStore,
)
from aitos.capital_intelligence.solana import SolanaAddressActivityAdapter
from aitos.capital_intelligence.sec_edgar import SecEdgarFilingsAdapter
from aitos.capital_intelligence.storage import ClickHouseCapitalStore
from aitos.capital_intelligence.token_registry import TokenMetadata, TokenRegistry

LOG = logging.getLogger("aitos.cfei")


def _json_env(name: str, default: Any) -> Any:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must contain valid JSON") from exc


class _NoPriceBook:
    async def quote(self, asset_id: str, at):
        return None


async def _metrics_server(port: int, counters: dict[str, int]) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.readline()
        while await reader.readline() not in (b"\r\n", b"\n", b""):
            pass
        lines = [
            "# HELP aitos_cfei_events_persisted Total normalized events appended.",
            "# TYPE aitos_cfei_events_persisted counter",
            f"aitos_cfei_events_persisted {counters['events']}",
            "# HELP aitos_cfei_poll_errors Total source poll errors.",
            "# TYPE aitos_cfei_poll_errors counter",
            f"aitos_cfei_poll_errors {counters['errors']}",
            "# HELP aitos_cfei_last_poll_unix_seconds Last successful poll timestamp.",
            "# TYPE aitos_cfei_last_poll_unix_seconds gauge",
            f"aitos_cfei_last_poll_unix_seconds {counters['last_poll']}",
        ]
        body = ("\n".join(lines) + "\n").encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    async with server:
        await server.serve_forever()


async def main() -> None:
    if os.getenv("AITOS_CFEI_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        raise SystemExit(
            "CFEI is disabled. Set AITOS_CFEI_ENABLED=true and use the capital-intelligence compose profile."
        )
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    redis_client = redis.from_url(
        os.getenv("REDIS_URL")
        or f"redis://:{os.environ['REDIS_PASSWORD']}@{os.getenv('REDIS_HOST', 'redis')}:{os.getenv('REDIS_PORT', '6379')}/0",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
        health_check_interval=30,
    )
    clickhouse = await clickhouse_connect.get_async_client(
        host=os.getenv("CLICKHOUSE_HOST", "clickhouse"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos"),
        secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
    )
    store = ClickHouseCapitalStore(
        clickhouse, os.getenv("CLICKHOUSE_DATABASE", "aitos")
    )
    await store.ensure_schema()
    checkpoints = RedisCheckpointStore(redis_client)
    workers: list[CapitalIngestionWorker] = []
    interval = float(os.getenv("AITOS_CFEI_POLL_SECONDS", "15"))

    token_config = _json_env("AITOS_CFEI_TOKENS", [])
    tokens = [
        TokenMetadata(
            chain_id=str(item["chain_id"]),
            address=str(item["address"]),
            asset_id=str(item["asset_id"]),
            symbol=str(item["symbol"]),
            decimals=int(item["decimals"]),
            price_symbol=item.get("price_symbol"),
            price_quote_currency=str(item.get("price_quote_currency", "USDT")),
        )
        for item in token_config
    ]
    registry = TokenRegistry(tokens) if tokens else None
    price_tokens = [item for item in tokens if item.price_symbol]
    price_book = (
        BinanceHistoricalPriceBook(price_tokens) if price_tokens else _NoPriceBook()
    )
    allow_usdt_proxy = os.getenv(
        "AITOS_CFEI_ALLOW_USDT_USD_PROXY", "false"
    ).lower() in {"1", "true", "yes"}

    async def event_enricher(event):
        return await enrich_capital_event(
            event,
            registry=registry,
            price_book=price_book,
            allow_usdt_usd_proxy=allow_usdt_proxy,
        )

    enricher = event_enricher if registry else None

    symbols = _json_env("AITOS_CFEI_BINANCE_SYMBOLS", [])
    if symbols:
        adapter = BinanceFuturesCapitalAdapter(symbols=symbols)
        workers.append(
            CapitalIngestionWorker(
                source_id="binance-usdm-aggregates",
                source=adapter,
                checkpoints=checkpoints,
                store=store,
                poll_interval_seconds=interval,
            )
        )

    evm_cfg = _json_env("AITOS_CFEI_EVM", [])
    for index, config in enumerate(evm_cfg):
        adapter = EVMLogCapitalAdapter(
            rpc_url=config["rpc_url"],
            chain_id=str(config["chain_id"]),
            contracts=config["contracts"],
            confirmations=int(config.get("confirmations", 12)),
            max_block_range=int(config.get("max_block_range", 250)),
            max_reorg_depth=int(config.get("max_reorg_depth", 64)),
        )
        workers.append(
            CapitalIngestionWorker(
                source_id=f"evm-{config['chain_id']}-{index}",
                source=adapter,
                checkpoints=checkpoints,
                store=store,
                poll_interval_seconds=interval,
                event_enricher=enricher,
            )
        )

    dex_cfg = _json_env("AITOS_CFEI_DEX_POOLS", [])
    for index, config in enumerate(dex_cfg):
        adapter = EvmDexSwapAdapter(
            rpc_url=config["rpc_url"],
            chain_id=str(config["chain_id"]),
            pools=config["pools"],
            confirmations=int(config.get("confirmations", 12)),
        )
        workers.append(
            CapitalIngestionWorker(
                source_id=f"dex-{config['chain_id']}-{index}",
                source=adapter,
                checkpoints=checkpoints,
                store=store,
                poll_interval_seconds=interval,
                event_enricher=enricher,
            )
        )

    sol_cfg = _json_env("AITOS_CFEI_SOLANA", [])
    for index, config in enumerate(sol_cfg):
        adapter = SolanaAddressActivityAdapter(
            rpc_url=config["rpc_url"],
            addresses=config["addresses"],
            page_limit=int(config.get("page_limit", 100)),
        )
        workers.append(
            CapitalIngestionWorker(
                source_id=f"solana-{index}",
                source=adapter,
                checkpoints=checkpoints,
                store=store,
                poll_interval_seconds=interval,
                event_enricher=enricher,
            )
        )

    sec_ciks = _json_env("AITOS_CFEI_SEC_CIKS", [])
    if sec_ciks:
        adapter = SecEdgarFilingsAdapter(
            ciks=[str(value) for value in sec_ciks],
            user_agent=os.getenv("AITOS_CFEI_SEC_USER_AGENT", ""),
            max_filings_per_poll=int(
                os.getenv("AITOS_CFEI_SEC_MAX_FILINGS_PER_POLL", "25")
            ),
        )
        workers.append(
            CapitalIngestionWorker(
                source_id="sec-edgar-disclosures",
                source=adapter,
                checkpoints=checkpoints,
                store=store,
                poll_interval_seconds=max(interval, 60.0),
            )
        )

    if not workers:
        raise SystemExit(
            "CFEI enabled but no observer sources configured; refusing an idle service."
        )

    counters = {"events": 0, "errors": 0, "last_poll": 0}
    original_run_once = CapitalIngestionWorker.run_once

    async def measured_run_once(worker):
        try:
            count = await original_run_once(worker)
            counters["events"] += count
            counters["last_poll"] = int(__import__("time").time())
            return count
        except Exception:
            counters["errors"] += 1
            raise

    for worker in workers:
        worker.run_once = lambda worker=worker: measured_run_once(worker)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    metrics = asyncio.create_task(
        _metrics_server(int(os.getenv("AITOS_CFEI_METRICS_PORT", "8121")), counters)
    )
    tasks = [asyncio.create_task(worker.run_forever()) for worker in workers]
    LOG.info(
        "CFEI observer started with %s source workers and %s token registry entries",
        len(workers),
        len(tokens),
    )
    await stop.wait()
    for worker in workers:
        worker.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    metrics.cancel()
    await asyncio.gather(metrics, return_exceptions=True)
    for worker in workers:
        await worker.close()
    if hasattr(price_book, "close"):
        await price_book.close()
    await clickhouse.close()
    await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
