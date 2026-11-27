"""Incremental Binance websocket SUBSCRIBE/UNSUBSCRIBE without reconnect storm.

Extracted so the large method can live outside ``binance.py`` while
``ManagedStreamSet`` and the adapter both share the same implementation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

from aitos.logging_setup import get_logger
from aitos.market_data.endpoints import BINANCE_USDM_WS_MAX_LIFETIME_SECONDS

logger = get_logger("aitos.exchange.connect_raw_dynamic")

INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
STABLE_CONNECTION_RESET_SECONDS = 60.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def connect_raw_dynamic(
    adapter: Any,
    get_streams: Callable[[], list[str]],
    changed: asyncio.Event,
    emit_reconnect: bool,
    label: str,
) -> AsyncIterator[tuple[Any, str]]:
    """Like ``_connect_raw``, but the subscribed stream set can change
    while the connection stays open.

    On every (re)connect, subscribes to whatever ``get_streams()``
    currently returns. While connected, whenever ``changed`` is set, the
    delta versus what is currently subscribed is sent as Binance
    SUBSCRIBE/UNSUBSCRIBE control frames over the *same* websocket — no
    reconnect, no orderbook re-bootstrap, no freshness gap. A reconnect
    only happens on genuine connection loss or the Binance-enforced
    lifecycle rotation, exactly as in ``_connect_raw``.
    """
    backoff = INITIAL_BACKOFF_SECONDS
    while True:
        streams = list(dict.fromkeys(get_streams()))
        if not streams:
            changed.clear()
            # Close the check/wait race: if a producer changed the desired
            # cohort just before clear(), observe it before sleeping.
            if get_streams():
                continue
            await changed.wait()
            continue
        base_url, _ = adapter._get_ws_base_url(streams)
        url = f"{base_url}?streams={'/'.join(streams)}"
        subscribed = set(streams)
        ws = None
        recv_task: asyncio.Task | None = None
        termination_class = "unknown"
        connection_started_monotonic: float | None = None
        try:
            started_at = _now_iso()
            connection_started_monotonic = asyncio.get_running_loop().time()
            transport = adapter._ws_transport
            connection_stats = transport.setdefault("managed_connections", {})
            stream_stats = connection_stats.setdefault(
                label,
                {
                    "connect_attempts": 0,
                    "connection_failure_count": 0,
                    "successful_handshakes": 0,
                    "close_count": 0,
                    "intentional_close_count": 0,
                    "normal_close_count": 0,
                    "abnormal_close_count": 0,
                    "lifecycle_rotation_count": 0,
                    "last_close_code": None,
                    "last_close_reason": None,
                    "last_disconnect_class": None,
                    "last_connection_uptime_seconds": None,
                },
            )
            stream_stats["connect_attempts"] += 1
            transport.update(
                {
                    "state": "connecting",
                    "current_url": url,
                    "streams": streams,
                    "connect_attempts": adapter._ws_transport["connect_attempts"] + 1,
                    "last_connect_started_at": started_at,
                    "last_error_type": None,
                    "last_error": None,
                    "subscription_mode": "managed_dynamic",
                }
            )
            logger.info(
                "Binance managed websocket connecting",
                extra={
                    "aitos_extra": {
                        "stage": "connect_started",
                        "url": url,
                        "streams": streams,
                        "label": label,
                    }
                },
            )
            async with adapter._ws_connector(url) as ws:
                handshake_at = _now_iso()
                stream_stats["successful_handshakes"] += 1
                adapter._ws_transport.update(
                    {
                        "state": "connected",
                        "successful_handshakes": adapter._ws_transport[
                            "successful_handshakes"
                        ]
                        + 1,
                        "last_handshake_at": handshake_at,
                    }
                )
                logger.info(
                    "Binance managed websocket connected",
                    extra={
                        "aitos_extra": {
                            "stage": "handshake_complete",
                            "url": url,
                            "label": label,
                            "subscription_mode": "managed_dynamic",
                        }
                    },
                )
                sub_request_id = 0
                first_message = True
                # Do not clear changed here: a universe update can arrive
                # while the TCP/WebSocket handshake is in progress. Clearing
                # it after the handshake loses that update and leaves the
                # connection subscribed to the stale bootstrap cohort.

                async def _recv() -> str:
                    return await ws.recv()

                async with asyncio.timeout(BINANCE_USDM_WS_MAX_LIFETIME_SECONDS):
                    while True:
                        recv_task = asyncio.create_task(_recv())
                        changed_task = asyncio.create_task(changed.wait())
                        done, pending = await asyncio.wait(
                            {recv_task, changed_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)

                        if changed_task in done and changed.is_set():
                            changed.clear()
                            desired = set(dict.fromkeys(get_streams()))
                            to_add = sorted(desired - subscribed)
                            to_remove = sorted(subscribed - desired)
                            if to_add:
                                sub_request_id += 1
                                await ws.send(
                                    json.dumps(
                                        {
                                            "method": "SUBSCRIBE",
                                            "params": to_add,
                                            "id": sub_request_id,
                                        }
                                    )
                                )
                            if to_remove:
                                sub_request_id += 1
                                await ws.send(
                                    json.dumps(
                                        {
                                            "method": "UNSUBSCRIBE",
                                            "params": to_remove,
                                            "id": sub_request_id,
                                        }
                                    )
                                )
                            if to_add or to_remove:
                                subscribed = desired
                                adapter._ws_transport["streams"] = list(desired)
                                logger.info(
                                    "Binance managed stream subscription delta applied",
                                    extra={
                                        "aitos_extra": {
                                            "stage": "subscribe_delta",
                                            "label": label,
                                            "added": to_add,
                                            "removed": to_remove,
                                        }
                                    },
                                )
                            if recv_task not in done:
                                continue

                        if recv_task in done:
                            try:
                                raw_message = recv_task.result()
                            except Exception:
                                raise
                            adapter._ws_transport["frames_received"] += 1
                            if first_message:
                                adapter._ws_transport["last_first_frame_at"] = (
                                    _now_iso()
                                )
                                first_message = False
                            envelope = json.loads(raw_message)
                            # Control-frame acks have no "stream" / "data"
                            if (
                                isinstance(envelope, dict)
                                and (
                                    "result" in envelope
                                    or envelope.get("id") is not None
                                )
                                and "stream" not in envelope
                                and "data" not in envelope
                            ):
                                continue
                            if "stream" in envelope or "data" in envelope:
                                payload = envelope.get("data", envelope)
                                stream_name = envelope.get("stream", "")
                            else:
                                payload, stream_name = envelope, ""
                            adapter._ws_transport["market_events_received"] += 1
                            adapter._ws_transport["last_market_event_at"] = _now_iso()
                            latency = getattr(ws, "latency", None)
                            if isinstance(latency, (int, float)) and latency >= 0:
                                rtt_ms = float(latency) * 1000.0
                                adapter._ws_transport["last_ping_rtt_ms"] = round(
                                    rtt_ms, 3
                                )
                                adapter._ws_transport["max_ping_rtt_ms"] = max(
                                    float(
                                        adapter._ws_transport.get(
                                            "max_ping_rtt_ms", 0.0
                                        )
                                    ),
                                    rtt_ms,
                                )
                            yield payload, stream_name
        except asyncio.CancelledError:
            termination_class = "intentional_cancel"
            if ws is not None:
                adapter._ws_transport.update(
                    {
                        "last_close_at": _now_iso(),
                        "last_close_code": getattr(ws, "close_code", None),
                        "last_close_reason": getattr(ws, "close_reason", None),
                        "last_disconnect_class": termination_class,
                        "last_close_stream": label,
                    }
                )
            raise
        # Python 3.10 exposes asyncio.TimeoutError separately from the
        # built-in TimeoutError. A websocket recv timeout is therefore a
        # lifecycle timeout and must not fall through to the generic error
        # state, otherwise managed-stream lifecycle rotation never occurs.
        except (TimeoutError, asyncio.TimeoutError):
            termination_class = "lifecycle_rotation"
            adapter._ws_transport.update(
                {
                    "state": "lifecycle_rotation",
                    "last_close_at": _now_iso(),
                    "last_close_code": getattr(ws, "close_code", None),
                    "last_close_reason": getattr(ws, "close_reason", None),
                    "last_disconnect_class": termination_class,
                    "last_close_stream": label,
                    "lifecycle_rotation_count": (
                        adapter._ws_transport.get("lifecycle_rotation_count", 0) + 1
                    ),
                }
            )
            logger.info(
                "Binance managed websocket lifecycle rotation",
                extra={"aitos_extra": {"stage": "lifecycle_rotation", "label": label}},
            )
        except Exception as exc:
            close_code = getattr(ws, "close_code", None)
            termination_class = (
                "connect_failure"
                if ws is None
                else (
                    "remote_clean_close"
                    if close_code in (1000, 1001)
                    else "abnormal_error"
                )
            )
            if ws is None:
                adapter._ws_transport["connection_failure_count"] = (
                    adapter._ws_transport.get("connection_failure_count", 0) + 1
                )
                stream_stats["connection_failure_count"] = (
                    int(stream_stats.get("connection_failure_count", 0)) + 1
                )
            adapter._ws_transport.update(
                {
                    "state": (
                        "reconnecting"
                        if termination_class == "remote_clean_close"
                        else "error"
                    ),
                    "last_close_code": getattr(ws, "close_code", None),
                    "last_close_reason": getattr(ws, "close_reason", None),
                    "last_disconnect_class": termination_class,
                    "last_close_stream": label,
                    "last_error_type": type(exc).__name__,
                    "last_error": str(exc),
                    "last_close_at": _now_iso(),
                }
            )
            logger.error(
                "Binance managed websocket error",
                extra={
                    "aitos_extra": {
                        "stage": "error",
                        "label": label,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                },
            )
        finally:
            if recv_task is not None and not recv_task.done():
                recv_task.cancel()
                await asyncio.gather(recv_task, return_exceptions=True)
            if ws is not None:
                transport = adapter._ws_transport
                transport["close_count"] = transport.get("close_count", 0) + 1
                transport["last_close_code"] = getattr(ws, "close_code", None)
                transport["last_close_reason"] = getattr(ws, "close_reason", None)
                transport["last_disconnect_class"] = termination_class
                transport["last_close_stream"] = label
                stream_stats = transport.setdefault(
                    "managed_connections", {}
                ).setdefault(label, {})
                stream_stats["close_count"] = (
                    int(stream_stats.get("close_count", 0)) + 1
                )
                stream_stats["last_close_code"] = transport["last_close_code"]
                stream_stats["last_close_reason"] = transport["last_close_reason"]
                stream_stats["last_disconnect_class"] = termination_class
                if connection_started_monotonic is not None:
                    uptime = max(
                        0.0,
                        asyncio.get_running_loop().time()
                        - connection_started_monotonic,
                    )
                    transport["last_connection_uptime_seconds"] = round(uptime, 3)
                    stream_stats["last_connection_uptime_seconds"] = round(uptime, 3)
                    if uptime >= STABLE_CONNECTION_RESET_SECONDS:
                        backoff = INITIAL_BACKOFF_SECONDS
                        transport["last_stable_connection_uptime_seconds"] = round(
                            uptime, 3
                        )
                counter = {
                    "intentional_cancel": "intentional_close_count",
                    "remote_clean_close": "normal_close_count",
                    "abnormal_error": "abnormal_close_count",
                }.get(termination_class)
                if counter is not None:
                    transport[counter] = transport.get(counter, 0) + 1
                    stream_counter = counter
                    stream_stats[stream_counter] = (
                        int(stream_stats.get(stream_counter, 0)) + 1
                    )
                elif termination_class == "lifecycle_rotation":
                    stream_stats["lifecycle_rotation_count"] = (
                        int(stream_stats.get("lifecycle_rotation_count", 0)) + 1
                    )
        if emit_reconnect:
            yield None, "__reconnect__"
        logger.warning(
            "Binance managed stream reconnecting",
            extra={
                "aitos_extra": {
                    "stage": "reconnect_wait",
                    "label": label,
                    "backoff_seconds": backoff,
                }
            },
        )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
