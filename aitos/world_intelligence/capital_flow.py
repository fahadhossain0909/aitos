"""Read-only bridge from persisted CFEI observations into World Intelligence."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import re
from math import isfinite, log10
from typing import Any
from urllib.parse import quote

from aitos.capital_intelligence.contracts import CapitalEvent
from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence


class CapitalFlowIntelligenceAdapter:
    """Expose CFEI observations as advisory WI events without assigning sentiment.

    The adapter reads only persisted ClickHouse rows and uses an observed-time /
    event-id cursor. It never creates a strategy signal or promotes wallet identity.
    """

    def __init__(
        self,
        *,
        clickhouse: Any,
        database: str = "aitos",
        source_id: str = "cfei-capital-flow",
        initial_lookback_hours: int = 24,
        max_events: int = 500,
        severity_reference_usd: float = 1_000_000.0,
    ):
        if not isinstance(database, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,63}", database
        ):
            raise ValueError("invalid ClickHouse database")
        if not source_id.strip():
            raise ValueError("source_id is required")
        if not 1 <= initial_lookback_hours <= 720:
            raise ValueError("initial lookback must be between 1 and 720 hours")
        if not 1 <= max_events <= 5000:
            raise ValueError("max_events must be between 1 and 5000")
        if not isfinite(severity_reference_usd) or severity_reference_usd <= 0:
            raise ValueError("severity_reference_usd must be positive and finite")
        self.clickhouse = clickhouse
        self.database = database
        self.source_id = source_id
        self.initial_lookback_hours = initial_lookback_hours
        self.max_events = max_events
        self.severity_reference_usd = float(severity_reference_usd)

    def _parse_cursor(self, cursor: str | None) -> tuple[str, str]:
        if cursor is None:
            start = datetime.now(timezone.utc) - timedelta(
                hours=self.initial_lookback_hours
            )
            return start.isoformat(), ""
        try:
            value = json.loads(cursor)
            stamp = datetime.fromisoformat(
                str(value["observed_time"]).replace("Z", "+00:00")
            )
            event_id = str(value["event_id"])
        except (TypeError, ValueError, KeyError):
            raise ValueError("invalid CFEI-to-WI cursor") from None
        if stamp.tzinfo is None:
            raise ValueError("CFEI cursor timestamp must be timezone-aware")
        return stamp.astimezone(timezone.utc).isoformat(), event_id

    @staticmethod
    def _event(payload_json: str) -> CapitalEvent:
        try:
            payload = json.loads(payload_json)
            payload["event_time"] = datetime.fromisoformat(
                payload["event_time"].replace("Z", "+00:00")
            )
            payload["observed_time"] = datetime.fromisoformat(
                payload["observed_time"].replace("Z", "+00:00")
            )
            return CapitalEvent(**payload)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid persisted CFEI event payload") from exc

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[IntelligenceEvent], str | None]:
        observed_after, event_after = self._parse_cursor(cursor)
        # Use bound parameters for cursor values. FINAL resolves replacing versions
        # so reorg tombstones supersede earlier confirmed event rows.
        query = """SELECT payload_json, observed_time, event_id
        FROM __DATABASE__.capital_flow_events FINAL
        WHERE observed_time > parseDateTime64BestEffort(%(observed_after)s)
           OR (observed_time = parseDateTime64BestEffort(%(observed_after)s)
               AND event_id > %(event_after)s)
        ORDER BY observed_time ASC, event_id ASC
        LIMIT %(limit)s""".replace(  # nosec B608 - database is validated as a SQL identifier.
            "__DATABASE__", self.database
        )
        result = await self.clickhouse.query(
            query,
            parameters={
                "observed_after": observed_after,
                "event_after": event_after,
                "limit": self.max_events,
            },
        )
        events: list[IntelligenceEvent] = []
        next_cursor = cursor
        for payload_json, _observed_at, _ in result.result_rows:
            event = self._event(payload_json)
            if event.finality == "reverted":
                # Reverted events are retained in CFEI's source of truth, but are
                # not forwarded as positive market context.
                next_cursor = json.dumps(
                    {
                        "observed_time": event.observed_time.isoformat(),
                        "event_id": event.event_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                continue
            if event.observed_time < event.event_time:
                raise RuntimeError("CFEI event observed before its event time")
            key = event.deduplication_key
            uri = f"aitos://capital-flow/{quote(key, safe='')}"
            value = event.usd_value
            severity = (
                min(1.0, log10(1.0 + value) / log10(1.0 + self.severity_reference_usd))
                if value is not None
                else 0.0
            )
            entities = ()
            if event.event_type != "dex_pool_created":
                instrument = str(event.instrument).strip()
                if instrument and not instrument.startswith("0x"):
                    entities = (instrument,)
            source = SourceEvidence(
                source_id=self.source_id,
                source_kind=f"capital_flow:{event.source_kind}",
                uri=uri,
                published_at=event.event_time,
                first_seen_at=event.observed_time,
                reliability=max(0.0, min(1.0, event.confidence)),
                is_primary=event.source_kind in {"onchain_transfer", "dex_swap"},
                original_story_id=key,
            )
            events.append(
                IntelligenceEvent(
                    event_id=event.event_id,
                    event_type="capital_flow_observation",
                    title=(
                        f"{event.event_type} observation for {event.instrument} "
                        f"at {event.venue}"
                    )[:500],
                    entities=entities,
                    published_at=event.event_time,
                    first_seen_at=event.observed_time,
                    source=source,
                    sentiment=0.0,
                    severity=severity,
                    novelty=0.1,
                    confidence=event.confidence,
                    verification=(
                        "primary_source" if source.is_primary else "unverified"
                    ),
                    impact_horizon="unknown",
                    metadata={
                        "cfei_schema_version": event.schema_version,
                        "cfei_source_id": event.source_id,
                        "market": event.market,
                        "venue": event.venue,
                        "instrument": event.instrument,
                        "source_kind": event.source_kind,
                        "event_type": event.event_type,
                        "quantity": event.quantity,
                        "unit": event.unit,
                        "usd_value": event.usd_value,
                        "actor_id": event.actor_id,
                        "counterparty_id": event.counterparty_id,
                        "transaction_id": event.transaction_id,
                        "block_number": event.block_number,
                        "block_hash": event.block_hash,
                        "finality": event.finality,
                        "flow_sign": event.metadata.get("flow_sign"),
                        "flow_direction": event.metadata.get("flow_direction"),
                        "identity_is_unverified": True,
                        "sentiment_not_inferred_from_transfer": True,
                        "strategy_use_approved": False,
                    },
                )
            )
            next_cursor = json.dumps(
                {
                    "observed_time": event.observed_time.isoformat(),
                    "event_id": event.event_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        if next_cursor is None:
            next_cursor = json.dumps(
                {"observed_time": observed_after, "event_id": event_after},
                sort_keys=True,
                separators=(",", ":"),
            )
        return events, next_cursor


__all__ = ["CapitalFlowIntelligenceAdapter"]
