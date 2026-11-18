"""Read-only, bounded entity explorer queries for CFEI.

The explorer exposes observed address activity only. It does not infer beneficial
ownership, merge identities across chains, or produce strategy decisions.
"""

from __future__ import annotations

import json
from typing import Any


class CapitalEntityExplorer:
    """Bounded ClickHouse read model for analyst-facing entity inspection."""

    def __init__(self, clickhouse: Any, database: str = "aitos"):
        if not database or not database.replace("_", "").isalnum():
            raise ValueError("invalid ClickHouse database")
        self.clickhouse = clickhouse
        self.database = database

    @property
    def events_table(self) -> str:
        return f"{self.database}.capital_flow_events"

    @property
    def evidence_table(self) -> str:
        return f"{self.database}.capital_entity_evidence"

    @property
    def labels_table(self) -> str:
        return f"{self.database}.capital_entity_labels"

    async def list_entities(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if isinstance(offset, bool) or not 0 <= offset <= 10_000:
            raise ValueError("offset must be between 0 and 10000")
        query = f"""
            WITH address_flows AS (
                SELECT
                    venue,
                    actor_id AS address,
                    usd_value,
                    JSONExtractInt(payload_json, 'metadata', 'flow_sign') AS flow_sign,
                    source_id,
                    counterparty_id AS counterparty,
                    observed_time
                FROM {self.events_table} FINAL
                WHERE actor_id != ''
                  AND lower(actor_id) NOT IN (
                      '0x0000000000000000000000000000000000000000',
                      '0x000000000000000000000000000000000000dead'
                  )
                  AND finality != 'reverted'
                  AND usd_value IS NOT NULL
                  AND JSONExtractString(
                        payload_json, 'metadata', 'actor_identity_kind'
                      ) IN ('evm_address', 'solana_address')
                  AND JSONExtractBool(
                        payload_json, 'metadata',
                        'actor_is_pool_caller_not_verified_beneficial_owner'
                      ) = 0
                  AND JSONExtractBool(
                        payload_json, 'metadata', 'aggregate_only'
                      ) = 0
                UNION ALL
                SELECT
                    venue,
                    counterparty_id AS address,
                    usd_value,
                    -JSONExtractInt(payload_json, 'metadata', 'flow_sign') AS flow_sign,
                    source_id,
                    actor_id AS counterparty,
                    observed_time
                FROM {self.events_table} FINAL
                WHERE counterparty_id != ''
                  AND lower(counterparty_id) NOT IN (
                      '0x0000000000000000000000000000000000000000',
                      '0x000000000000000000000000000000000000dead'
                  )
                  AND actor_id != ''
                  AND finality != 'reverted'
                  AND usd_value IS NOT NULL
                  AND JSONExtractString(
                        payload_json, 'metadata', 'actor_identity_kind'
                      ) IN ('evm_address', 'solana_address')
                  AND JSONExtractBool(
                        payload_json, 'metadata',
                        'actor_is_pool_caller_not_verified_beneficial_owner'
                      ) = 0
                  AND JSONExtractBool(
                        payload_json, 'metadata', 'aggregate_only'
                      ) = 0
            )
            SELECT
                venue,
                address,
                count() AS event_count,
                sum(ifNull(usd_value, 0)) AS gross_flow_usd,
                sumIf(ifNull(usd_value, 0), flow_sign = 1) AS inflow_usd,
                sumIf(ifNull(usd_value, 0), flow_sign = -1) AS outflow_usd,
                uniqExactIf(source_id, source_id != '') AS source_count,
                uniqExactIf(counterparty, counterparty != '') AS counterparty_count,
                max(observed_time) AS last_observed_at
            FROM address_flows
            GROUP BY venue, address
            ORDER BY gross_flow_usd DESC, venue ASC, address ASC
            LIMIT {{limit:UInt32}} OFFSET {{offset:UInt32}}
        """
        result = await self.clickhouse.query(
            query, parameters={"limit": limit, "offset": offset}
        )
        return [
            {
                "chain": row[0],
                "address": row[1],
                "entity_id": f"{row[0]}:{row[1]}",
                "event_count": int(row[2]),
                "gross_flow_usd": float(row[3]),
                "inflow_usd": float(row[4]),
                "outflow_usd": float(row[5]),
                "source_count": int(row[6]),
                "counterparty_count": int(row[7]),
                "last_observed_at": row[8].isoformat()
                if hasattr(row[8], "isoformat")
                else str(row[8]),
                "identity_status": "unverified_address",
            }
            for row in result.result_rows
        ]

    async def get_entity(
        self, chain: str, address: str, *, event_limit: int = 100
    ) -> dict[str, Any] | None:
        if not chain or len(chain) > 64 or any(c.isspace() for c in chain):
            raise ValueError("invalid chain identifier")
        if not address or len(address) > 128 or any(c.isspace() for c in address):
            raise ValueError("invalid address")
        if isinstance(event_limit, bool) or not 1 <= event_limit <= 500:
            raise ValueError("event_limit must be between 1 and 500")

        entity_key = f"{chain}:{address}"
        events_result = await self.clickhouse.query(
            f"""
            SELECT payload_json
            FROM {self.events_table} FINAL
            WHERE venue = {{chain:String}}
              AND (actor_id = {{address:String}} OR counterparty_id = {{address:String}})
              AND finality != 'reverted'
              AND JSONExtractString(
                    payload_json, 'metadata', 'actor_identity_kind'
                  ) IN ('evm_address', 'solana_address')
            ORDER BY event_time DESC, observed_time DESC
            LIMIT {{limit:UInt32}}
            """,
            parameters={"chain": chain, "address": address, "limit": event_limit},
        )
        if not events_result.result_rows:
            return None

        evidence_result = await self.clickhouse.query(
            f"""
            SELECT subject_id, object_id, relation, evidence_type, evidence_ref,
                   observed_at, confidence, status
            FROM {self.evidence_table} FINAL
            WHERE subject_id = {{entity_id:String}} OR object_id = {{entity_id:String}}
            ORDER BY observed_at DESC
            LIMIT 200
            """,
            parameters={"entity_id": entity_key},
        )
        labels_result = await self.clickhouse.query(
            f"""
            SELECT label, category, source, verified, valid_from, valid_until
            FROM {self.labels_table} FINAL
            WHERE entity_id = {{entity_id:String}}
            ORDER BY valid_from DESC
            LIMIT 100
            """,
            parameters={"entity_id": entity_key},
        )

        events = []
        for (payload_json,) in events_result.result_rows:
            try:
                event = json.loads(payload_json)
                base_sign = event.get("metadata", {}).get("flow_sign", 0)
                if base_sign not in (-1, 0, 1):
                    base_sign = 0
                if event.get("actor_id") == address:
                    event["entity_role"] = "actor"
                    event["entity_flow_sign"] = base_sign
                elif event.get("counterparty_id") == address:
                    event["entity_role"] = "counterparty"
                    event["entity_flow_sign"] = -base_sign
                events.append(event)
            except (TypeError, ValueError):
                raise RuntimeError("stored CFEI event payload is invalid") from None

        return {
            "chain": chain,
            "address": address,
            "entity_id": entity_key,
            "identity_status": "unverified_address",
            "beneficial_owner_verified": False,
            "events": events,
            "evidence": [
                {
                    "subject_id": row[0],
                    "object_id": row[1],
                    "relation": row[2],
                    "evidence_type": row[3],
                    "evidence_ref": row[4],
                    "observed_at": row[5].isoformat()
                    if hasattr(row[5], "isoformat")
                    else str(row[5]),
                    "confidence": float(row[6]),
                    "status": row[7],
                }
                for row in evidence_result.result_rows
            ],
            "labels": [
                {
                    "label": row[0],
                    "category": row[1],
                    "source": row[2],
                    "verified": bool(row[3]),
                    "valid_from": row[4].isoformat()
                    if hasattr(row[4], "isoformat")
                    else str(row[4]),
                    "valid_until": (
                        row[5].isoformat()
                        if hasattr(row[5], "isoformat")
                        else str(row[5]) if row[5] is not None else None
                    ),
                }
                for row in labels_result.result_rows
            ],
        }


__all__ = ["CapitalEntityExplorer"]
