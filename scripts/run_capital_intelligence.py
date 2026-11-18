"""Opt-in standalone CFEI observer process. It never imports trading execution."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import signal
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import clickhouse_connect
import redis.asyncio as redis
from neo4j import AsyncGraphDatabase

from aitos.capital_intelligence.alerts import CapitalFlowWebhookAlerter
from aitos.capital_intelligence.binance import BinanceFuturesCapitalAdapter
from aitos.capital_intelligence.binance_market import BinanceMarketStructureAdapter
from aitos.capital_intelligence.binance_price import BinanceHistoricalPriceBook
from aitos.capital_intelligence.cftc import CftcCotAdapter
from aitos.capital_intelligence.contracts import EntityLabel
from aitos.capital_intelligence.dex import EvmDexSwapAdapter
from aitos.capital_intelligence.dex_factory import EvmDexFactoryDiscoveryAdapter
from aitos.capital_intelligence.enrichment import enrich_capital_event
from aitos.capital_intelligence.evm import EVMLogCapitalAdapter
from aitos.capital_intelligence.evm_token_metadata import EvmTokenMetadataResolver
from aitos.capital_intelligence.explorer import CapitalEntityExplorer
from aitos.capital_intelligence.neo4j_graph import Neo4jEntityGraphStore
from aitos.capital_intelligence.runtime import (
    CapitalIngestionWorker,
    RedisCheckpointStore,
)
from aitos.capital_intelligence.sec_edgar import (
    SecEdgarArchiveBackfillSource,
    SecEdgarFilingsAdapter,
)
from aitos.capital_intelligence.solana import SolanaAddressActivityAdapter
from aitos.capital_intelligence.storage import ClickHouseCapitalStore
from aitos.capital_intelligence.token_registry import TokenMetadata, TokenRegistry
from aitos.capital_intelligence.watchlist import RedisEntityWatchlist
from aitos.core.contracts import Event, EventPriority
from aitos.eventbus.redis_bus import EventBus
from aitos.world_intelligence.capital_flow import CapitalFlowIntelligenceAdapter
from aitos.world_intelligence.lease import RedisSourceLeaseManager
from aitos.world_intelligence.storage import ClickHouseIntelligenceStore
from aitos.world_intelligence.worker import IntelligenceIngestionWorker

LOG = logging.getLogger("aitos.cfei")


def _json_env(name: str, default: Any) -> Any:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must contain valid JSON") from exc


def _configured_entity_labels(raw: Any) -> list[EntityLabel]:
    """Parse explicit operator classifications without inferring wallet identity."""
    if not isinstance(raw, list) or len(raw) > 5000:
        raise ValueError(
            "AITOS_CFEI_ENTITY_LABELS must be a list of at most 5000 items"
        )
    allowed_categories = {
        "exchange",
        "custodian",
        "bridge",
        "protocol",
        "market_maker",
        "treasury",
        "staking",
        "burn",
        "other",
    }
    labels = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each CFEI entity label must be an object")
        chain = str(item.get("chain", "")).strip()
        address = str(item.get("address", "")).strip()
        category = str(item.get("category", "")).strip().lower()
        label = str(item.get("label", "")).strip()
        source = str(item.get("source", "")).strip()
        verified = item.get("verified", False)
        if (
            not chain
            or len(chain) > 64
            or any(char.isspace() for char in chain)
            or not address
            or len(address) > 128
            or any(char.isspace() for char in address)
            or category not in allowed_categories
            or not label
            or not source
            or not isinstance(verified, bool)
        ):
            raise ValueError("invalid CFEI operator entity label")
        try:
            valid_from = datetime.fromisoformat(
                str(item["valid_from"]).replace("Z", "+00:00")
            )
            valid_until_raw = item.get("valid_until")
            valid_until = (
                datetime.fromisoformat(str(valid_until_raw).replace("Z", "+00:00"))
                if valid_until_raw
                else None
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                "CFEI entity labels require ISO-8601 validity dates"
            ) from None
        if valid_from.tzinfo is None or (
            valid_until is not None and valid_until.tzinfo is None
        ):
            raise ValueError("CFEI entity label timestamps must include a timezone")
        labels.append(
            EntityLabel(
                entity_id=f"{chain}:{address}",
                label=label,
                category=category,
                source=source,
                verified=verified,
                valid_from=valid_from.astimezone(timezone.utc),
                valid_until=(
                    valid_until.astimezone(timezone.utc) if valid_until else None
                ),
            )
        )
    return labels


class _NoPriceBook:
    async def quote(self, asset_id: str, at):
        return None


class _WorldIntelligenceEventSink:
    """Publish normalized WI events through AITOS' canonical Redis EventBus."""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        await self.event_bus.publish(
            Event(
                topic=topic,
                payload=dict(payload),
                source_module="world-intelligence-cfei",
                priority=EventPriority.NORMAL,
            )
        )


def _entity_dashboard_html() -> bytes:
    """Serve a dependency-free analyst UI; the bearer token stays in memory."""
    page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>AITOS Capital Flow Explorer</title>
<style>
:root{color-scheme:dark;font:14px system-ui,sans-serif;background:#10151d;color:#e7edf5}
body{max-width:1200px;margin:2rem auto;padding:0 1rem}
header{display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap}
h1{font-size:1.5rem}p,small{color:#aab7c7}
input,button{background:#1b2634;color:#e7edf5;border:1px solid #40536b;border-radius:6px;padding:.65rem}
button{cursor:pointer}button:hover{background:#29394c}
input{min-width:18rem;max-width:100%}
.layout{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(300px,.8fr);gap:1rem}
section{background:#17212d;border:1px solid #2c3b4d;border-radius:10px;padding:1rem;min-width:0}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:.6rem .4rem;border-bottom:1px solid #2c3b4d;overflow-wrap:anywhere}
th{color:#9fb3ca}td button{padding:.25rem .4rem}
#status{min-height:1.5rem;color:#f0c879}
pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:65vh;overflow:auto}
@media(max-width:800px){.layout{grid-template-columns:1fr}body{margin:1rem auto}}
</style>
</head>
<body>
<header><div><h1>Capital Flow &amp; Entity Explorer</h1>
<p>Observed address activity only. Identity and beneficial ownership are unverified.</p></div>
<div><input id="token" type="password" autocomplete="off" placeholder="Operator bearer token">
<button id="load">Load entities</button></div></header>
<p id="status" role="status">Enter the configured read-only API token to inspect observed entities.</p>
<div class="layout"><section><h2>Ranked observed addresses</h2>
<table><thead><tr><th>Chain</th><th>Address</th><th>Gross USD</th><th>Inflow</th><th>Outflow</th><th>Events</th></tr></thead>
<tbody id="entities"><tr><td colspan="6">No data loaded</td></tr></tbody></table></section>
<section><h2>Entity evidence</h2><p id="identity">Select an address to inspect its bounded history.</p>
<pre id="detail">No entity selected.</pre></section></div>
<script>
const token=document.getElementById('token'),status=document.getElementById('status');
const tbody=document.getElementById('entities'),detail=document.getElementById('detail');
function cell(row,value){const td=document.createElement('td');td.textContent=String(value??'—');row.appendChild(td);return td}
async function api(path){const response=await fetch(path,{headers:{Authorization:'Bearer '+token.value},cache:'no-store'});
if(!response.ok)throw new Error(response.status===401?'Unauthorized token':('API error '+response.status));return response.json()}
async function inspect(entity){try{status.textContent='Loading entity evidence…';
const data=await api('/v1/entities/'+encodeURIComponent(entity.chain)+'/'+encodeURIComponent(entity.address));
document.getElementById('identity').textContent=data.identity_status+' · beneficial owner verified: '+data.beneficial_owner_verified;
detail.textContent=JSON.stringify({entity_id:data.entity_id,events:data.events,evidence:data.evidence,labels:data.labels},null,2);
status.textContent='Entity history loaded. All labels remain evidence, not proof of ownership.'}
catch(error){status.textContent=error.message}}
async function load(){if(!token.value){status.textContent='Enter the API token first.';return}
try{status.textContent='Loading observed entities…';const data=await api('/v1/entities?limit=100&offset=0');tbody.replaceChildren();
for(const entity of data.entities){const tr=document.createElement('tr');cell(tr,entity.chain);
const address=cell(tr,'');const link=document.createElement('button');link.type='button';link.textContent=entity.address;
link.addEventListener('click',()=>inspect(entity));address.appendChild(link);
cell(tr,Number(entity.gross_flow_usd).toLocaleString(undefined,{maximumFractionDigits:2}));
cell(tr,Number(entity.inflow_usd).toLocaleString(undefined,{maximumFractionDigits:2}));
cell(tr,Number(entity.outflow_usd).toLocaleString(undefined,{maximumFractionDigits:2}));cell(tr,entity.event_count);tbody.appendChild(tr)}
if(!data.entities.length){const tr=document.createElement('tr');cell(tr,'No observed entities');tbody.appendChild(tr)}
status.textContent='Loaded '+data.entities.length+' observed addresses. Rankings are not a trading signal.'}
catch(error){status.textContent=error.message}}
document.getElementById('load').addEventListener('click',load);
token.addEventListener('keydown',event=>{if(event.key==='Enter')load()});
</script></body></html>"""
    return page.encode("utf-8")


def _render_metrics(counters: dict[str, Any]) -> bytes:
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
        "# HELP aitos_cfei_source_events_persisted_total Events persisted by source.",
        "# TYPE aitos_cfei_source_events_persisted_total counter",
        "# HELP aitos_cfei_source_poll_errors_total Poll failures by source.",
        "# TYPE aitos_cfei_source_poll_errors_total counter",
        "# HELP aitos_cfei_source_last_success_unix_seconds Last successful poll by source.",
        "# TYPE aitos_cfei_source_last_success_unix_seconds gauge",
        "# HELP aitos_cfei_source_lease_contention_total Source poll lease contention.",
        "# TYPE aitos_cfei_source_lease_contention_total counter",
    ]
    for source_id, values in sorted(counters.get("sources", {}).items()):
        # Prometheus label escaping: source IDs come from operator config.
        label = (
            str(source_id).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
        )
        labels = f'source_id="{label}"'
        lines.extend(
            [
                f"aitos_cfei_source_events_persisted_total{{{labels}}} {values['events']}",
                f"aitos_cfei_source_poll_errors_total{{{labels}}} {values['errors']}",
                f"aitos_cfei_source_last_success_unix_seconds{{{labels}}} {values['last_success']}",
                f"aitos_cfei_source_lease_contention_total{{{labels}}} {values['lease_contention']}",
            ]
        )
    return ("\n".join(lines) + "\n").encode()


async def _metrics_server(
    port: int,
    counters: dict[str, Any],
    *,
    explorer: CapitalEntityExplorer,
    api_token: str,
    watchlist: RedisEntityWatchlist,
) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        status = "200 OK"
        content_type = "text/plain; version=0.0.4"
        body = b""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            if len(request_line) > 8192:
                raise ValueError("request line too large")
            parts = request_line.decode("latin-1").strip().split()
            if len(parts) != 3:
                raise ValueError("invalid HTTP request")
            method, target, _version = parts
            headers = {}
            total_header_bytes = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                total_header_bytes += len(line)
                if total_header_bytes > 16_384:
                    raise ValueError("HTTP headers too large")
                if line in (b"\r\n", b"\n", b""):
                    break
                key, separator, value = line.decode("latin-1").partition(":")
                if separator:
                    headers[key.strip().lower()] = value.strip()

            parsed = urlsplit(target)
            request_body = b""
            if method in {"POST", "DELETE"}:
                try:
                    content_length = int(headers.get("content-length", "0"))
                except ValueError:
                    raise ValueError("invalid content length") from None
                if not 0 <= content_length <= 8192:
                    raise ValueError("request body too large")
                if content_length:
                    request_body = await asyncio.wait_for(
                        reader.readexactly(content_length), timeout=5
                    )
            if method not in {"GET", "POST", "DELETE"}:
                status = "405 Method Not Allowed"
                content_type = "application/json"
                body = b'{"error":"method_not_allowed"}'
            elif parsed.path == "/metrics":
                body = _render_metrics(counters)
            elif parsed.path == "/entities":
                content_type = "text/html; charset=utf-8"
                body = _entity_dashboard_html()
            elif parsed.path == "/v1/watchlist":
                content_type = "application/json"
                supplied = headers.get("authorization", "")
                expected = f"Bearer {api_token}" if api_token else ""
                if not api_token:
                    status = "404 Not Found"
                    body = b'{"error":"not_found"}'
                elif not hmac.compare_digest(supplied, expected):
                    status = "401 Unauthorized"
                    body = b'{"error":"unauthorized"}'
                elif method == "GET":
                    params = parse_qs(parsed.query, strict_parsing=False)
                    limit = int(params.get("limit", ["500"])[0])
                    entries = await watchlist.list(limit=limit)
                    body = json.dumps(
                        {"watchlist": entries}, separators=(",", ":"), allow_nan=False
                    ).encode()
                else:
                    try:
                        payload = json.loads(request_body or b"{}")
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        raise ValueError("invalid JSON body") from None
                    if not isinstance(payload, dict):
                        raise ValueError("request body must be an object")
                    chain = payload.get("chain", "")
                    address = payload.get("address", "")
                    if method == "POST":
                        entry = await watchlist.add(
                            chain, address, payload.get("note", "")
                        )
                        body = json.dumps(
                            {"watchlist_entry": entry},
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode()
                        status = "201 Created"
                    else:
                        removed = await watchlist.remove(chain, address)
                        body = json.dumps(
                            {"removed": removed},
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode()
            elif parsed.path == "/v1/entities" or parsed.path.startswith(
                "/v1/entities/"
            ):
                content_type = "application/json"
                supplied = headers.get("authorization", "")
                expected = f"Bearer {api_token}" if api_token else ""
                if not api_token:
                    status = "404 Not Found"
                    body = b'{"error":"not_found"}'
                elif not hmac.compare_digest(supplied, expected):
                    status = "401 Unauthorized"
                    body = b'{"error":"unauthorized"}'
                elif method != "GET":
                    status = "405 Method Not Allowed"
                    body = b'{"error":"method_not_allowed"}'
                elif parsed.path == "/v1/entities":
                    params = parse_qs(parsed.query, strict_parsing=False)
                    limit = int(params.get("limit", ["50"])[0])
                    offset = int(params.get("offset", ["0"])[0])
                    result = await explorer.list_entities(limit=limit, offset=offset)
                    body = json.dumps(
                        {"entities": result}, separators=(",", ":"), allow_nan=False
                    ).encode()
                else:
                    segments = parsed.path.split("/")
                    if len(segments) != 5 or segments[1:3] != ["v1", "entities"]:
                        status = "404 Not Found"
                        body = b'{"error":"not_found"}'
                    else:
                        chain, address = unquote(segments[3]), unquote(segments[4])
                        result = await explorer.get_entity(chain, address)
                        if result is None:
                            status = "404 Not Found"
                            body = b'{"error":"entity_not_found"}'
                        else:
                            body = json.dumps(
                                result, separators=(",", ":"), allow_nan=False
                            ).encode()
            else:
                status = "404 Not Found"
                content_type = "application/json"
                body = b'{"error":"not_found"}'
        except ValueError:
            status = "400 Bad Request"
            content_type = "application/json"
            body = b'{"error":"invalid_request"}'
        except Exception:
            LOG.exception("CFEI read-only API request failed")
            status = "500 Internal Server Error"
            content_type = "application/json"
            body = b'{"error":"internal_error"}'

        writer.write(
            f"HTTP/1.1 {status}\r\n".encode()
            + f"Content-Type: {content_type}\r\n".encode()
            + f"Content-Length: {len(body)}\r\nX-Content-Type-Options: nosniff\r\nCache-Control: no-store\r\nContent-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'\r\nConnection: close\r\n\r\n".encode()
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
    operator_labels = _configured_entity_labels(
        _json_env("AITOS_CFEI_ENTITY_LABELS", [])
    )
    if operator_labels:
        await store.append_entity_labels(operator_labels)
        LOG.info("Loaded %s operator-supplied CFEI entity labels", len(operator_labels))
    checkpoints = RedisCheckpointStore(redis_client)
    graph_store = None
    if os.getenv("AITOS_CFEI_NEO4J_ENABLED", "false").lower() in {"1", "true", "yes"}:
        neo4j_uri = os.getenv("NEO4J_URI", "")
        neo4j_user = os.getenv("NEO4J_USER", "")
        neo4j_password = os.getenv("NEO4J_PASSWORD", "")
        if not all((neo4j_uri, neo4j_user, neo4j_password)):
            raise SystemExit(
                "CFEI Neo4j enabled but NEO4J_URI/USER/PASSWORD are incomplete."
            )
        graph_driver = AsyncGraphDatabase.driver(
            neo4j_uri, auth=(neo4j_user, neo4j_password)
        )
        await graph_driver.verify_connectivity()
        graph_store = Neo4jEntityGraphStore(
            graph_driver, database=os.getenv("NEO4J_DATABASE", "neo4j")
        )
        await graph_store.ensure_schema()
        LOG.info("CFEI Neo4j evidence graph schema is ready")
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
    metadata_resolvers = []
    discovery_config = _json_env("AITOS_CFEI_EVM_TOKEN_DISCOVERY", [])
    discovered_tokens = []
    for config in discovery_config:
        resolver = EvmTokenMetadataResolver(
            rpc_url=config["rpc_url"],
            chain_id=str(config["chain_id"]),
            max_concurrency=int(config.get("max_concurrency", 4)),
        )
        metadata_resolvers.append(resolver)
        discovered_tokens.extend(await resolver.resolve_many(config["addresses"]))
    # Explicit registry entries override discovered metadata for the same
    # chain/address; discovery never supplies a trusted price-pair symbol.
    token_map = {
        (item.chain_id.lower(), item.address.lower()): item
        for item in discovered_tokens
    }
    token_map.update(
        {(item.chain_id.lower(), item.address.lower()): item for item in tokens}
    )
    tokens = list(token_map.values())
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

    # Optional, separately budgeted market microstructure observations.
    market_symbols = _json_env("AITOS_CFEI_BINANCE_MARKET_SYMBOLS", [])
    if market_symbols:
        market_adapter = BinanceMarketStructureAdapter(
            symbols=market_symbols,
            min_trade_notional_usd=float(
                os.getenv("AITOS_CFEI_LARGE_TRADE_USD", "100000")
            ),
        )
        workers.append(
            CapitalIngestionWorker(
                source_id="binance-usdm-market-structure",
                source=market_adapter,
                checkpoints=checkpoints,
                store=store,
                poll_interval_seconds=max(interval, 15.0),
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
            start_block=(
                int(config["start_block"])
                if config.get("start_block") is not None
                else None
            ),
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
            v4_pool_tokens=config.get("v4_pool_tokens", {}),
            confirmations=int(config.get("confirmations", 12)),
            max_reorg_depth=int(config.get("max_reorg_depth", 64)),
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

    dex_factory_cfg = _json_env("AITOS_CFEI_DEX_FACTORIES", [])
    for index, config in enumerate(dex_factory_cfg):
        adapter = EvmDexFactoryDiscoveryAdapter(
            rpc_url=config["rpc_url"],
            chain_id=str(config["chain_id"]),
            factories=config["factories"],
            confirmations=int(config.get("confirmations", 12)),
            max_block_range=int(config.get("max_block_range", 500)),
            max_reorg_depth=int(config.get("max_reorg_depth", 64)),
        )
        workers.append(
            CapitalIngestionWorker(
                source_id=f"dex-factory-{config['chain_id']}-{index}",
                source=adapter,
                checkpoints=checkpoints,
                store=store,
                poll_interval_seconds=max(interval, 30.0),
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

    cftc_cfg = _json_env("AITOS_CFEI_CFTC_COT", [])
    for index, config in enumerate(cftc_cfg):
        adapter = CftcCotAdapter(
            dataset_id=config["dataset_id"],
            field_map=config["field_map"],
            report_type=config.get("report_type", "configured_cot_report"),
            market=config.get("market", "futures"),
            page_limit=int(config.get("page_limit", 1000)),
            revision_refresh_pages_per_poll=int(
                config.get("revision_refresh_pages_per_poll", 5)
            ),
        )
        workers.append(
            CapitalIngestionWorker(
                source_id=f"cftc-cot-{config['dataset_id']}-{index}",
                source=adapter,
                checkpoints=checkpoints,
                store=store,
                poll_interval_seconds=max(interval, 3600.0),
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
            revision_rescan_filings_per_poll=int(
                os.getenv("AITOS_CFEI_SEC_REVISION_RESCAN_FILINGS", "5")
            ),
        )
        workers.append(
            CapitalIngestionWorker(
                source_id="sec-edgar-disclosures",
                source=adapter,
                checkpoints=checkpoints,
                store=store,
                poll_interval_seconds=max(interval, 60.0),
                batch_limit=50_000,
            )
        )
        if os.getenv("AITOS_CFEI_SEC_ARCHIVE_BACKFILL_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
        }:
            archive_source = SecEdgarArchiveBackfillSource(
                adapter,
                max_filings_per_poll=int(
                    os.getenv("AITOS_CFEI_SEC_ARCHIVE_MAX_FILINGS_PER_POLL", "1")
                ),
                max_rows_per_poll=int(
                    os.getenv("AITOS_CFEI_SEC_ARCHIVE_MAX_ROWS_PER_POLL", "500")
                ),
            )
            workers.append(
                CapitalIngestionWorker(
                    source_id="sec-edgar-archive-backfill",
                    source=archive_source,
                    checkpoints=checkpoints,
                    store=store,
                    poll_interval_seconds=max(interval, 60.0),
                    batch_limit=50_000,
                )
            )

    # Graph persistence is a replay-safe secondary sink. If it fails, the
    # Redis cursor is not committed and ClickHouse/Neo4j writes are replayed.
    for worker in workers:
        worker.graph_store = graph_store

    if not workers:
        raise SystemExit(
            "CFEI enabled but no observer sources configured; refusing an idle service."
        )

    counters = {"events": 0, "errors": 0, "last_poll": 0, "sources": {}}
    original_run_once = CapitalIngestionWorker.run_once

    async def measured_run_once(worker):
        source_metrics = counters["sources"].setdefault(
            worker.source_id,
            {"events": 0, "errors": 0, "last_success": 0, "lease_contention": 0},
        )
        try:
            count = await original_run_once(worker)
            if not worker.last_run_acquired_lease:
                source_metrics["lease_contention"] += 1
                return count
            now = int(__import__("time").time())
            counters["events"] += count
            counters["last_poll"] = now
            source_metrics["events"] += count
            source_metrics["last_success"] = now
            return count
        except Exception:
            counters["errors"] += 1
            source_metrics["errors"] += 1
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

    api_token = os.getenv("AITOS_CFEI_API_TOKEN", "")
    if api_token and len(api_token) < 32:
        raise SystemExit("AITOS_CFEI_API_TOKEN must contain at least 32 characters.")
    explorer = CapitalEntityExplorer(
        clickhouse, os.getenv("CLICKHOUSE_DATABASE", "aitos")
    )
    watchlist = RedisEntityWatchlist(
        redis_client,
        max_entries=int(os.getenv("AITOS_CFEI_WATCHLIST_MAX_ENTRIES", "5000")),
    )
    alert_webhook_url = os.getenv("AITOS_CFEI_ALERT_WEBHOOK_URL", "").strip()
    alerter = (
        CapitalFlowWebhookAlerter(
            redis_client=redis_client,
            watchlist=watchlist,
            webhook_url=alert_webhook_url,
            timeout_seconds=float(os.getenv("AITOS_CFEI_ALERT_TIMEOUT_SECONDS", "8")),
        )
        if alert_webhook_url
        else None
    )
    if alerter is not None:
        original_store_append = store.append

        async def append_with_watchlist_alerts(events):
            await original_store_append(events)
            for event in events:
                await alerter.notify_event(event)

        store.append = append_with_watchlist_alerts
        LOG.info("CFEI watchlist webhook alert delivery enabled")
    metrics = asyncio.create_task(
        _metrics_server(
            int(os.getenv("AITOS_CFEI_METRICS_PORT", "8121")),
            counters,
            explorer=explorer,
            api_token=api_token,
            watchlist=watchlist,
        )
    )
    tasks = [asyncio.create_task(worker.run_forever()) for worker in workers]

    wi_bridge_worker = None
    wi_bridge_task = None
    wi_event_bus = None
    if os.getenv("AITOS_CFEI_WI_BRIDGE_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
    }:
        wi_event_bus = EventBus(redis_client, module_id="cfei-world-intelligence-bus")
        await wi_event_bus.initialize({})
        wi_store = ClickHouseIntelligenceStore(
            clickhouse, os.getenv("CLICKHOUSE_DATABASE", "aitos")
        )
        await wi_store.ensure_schema()
        wi_adapter = CapitalFlowIntelligenceAdapter(
            clickhouse=clickhouse,
            database=os.getenv("CLICKHOUSE_DATABASE", "aitos"),
            source_id=os.getenv("AITOS_CFEI_WI_BRIDGE_SOURCE_ID", "cfei-capital-flow"),
            initial_lookback_hours=int(
                os.getenv("AITOS_CFEI_WI_BRIDGE_INITIAL_LOOKBACK_HOURS", "24")
            ),
            max_events=int(os.getenv("AITOS_CFEI_WI_BRIDGE_MAX_EVENTS", "500")),
        )
        wi_lease_manager = RedisSourceLeaseManager(
            redis_client, namespace="aitos:wiats:cfei-bridge:lease"
        )
        wi_bridge_worker = IntelligenceIngestionWorker(
            [wi_adapter],
            wi_store,
            _WorldIntelligenceEventSink(wi_event_bus),
            poll_interval_seconds=max(
                1.0,
                float(os.getenv("AITOS_CFEI_WI_BRIDGE_POLL_SECONDS", "30")),
            ),
            lease_manager=wi_lease_manager,
        )
        wi_bridge_task = asyncio.create_task(wi_bridge_worker.run_forever())
        LOG.info("CFEI-to-World-Intelligence bridge worker enabled")
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
    if wi_bridge_worker is not None:
        wi_bridge_worker.stop()
    if wi_bridge_task is not None:
        try:
            await asyncio.wait_for(
                asyncio.gather(wi_bridge_task, return_exceptions=True),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            wi_bridge_task.cancel()
            await asyncio.gather(wi_bridge_task, return_exceptions=True)
    if wi_event_bus is not None:
        await wi_event_bus.shutdown()
    metrics.cancel()
    await asyncio.gather(metrics, return_exceptions=True)
    for worker in workers:
        await worker.close()
    if alerter is not None:
        await alerter.close()
    if hasattr(price_book, "close"):
        await price_book.close()
    for resolver in metadata_resolvers:
        await resolver.close()
    if graph_store is not None:
        await graph_store.close()
    await clickhouse.close()
    await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
