"""ClickHouse persistence adapter for AMT session snapshots.

The adapter is intentionally separate from AMT measurement logic. It stores a
compact JSON representation so the schema can evolve without changing the
core engine.
"""

from __future__ import annotations

import json
from typing import Any

from .session_store import SessionSnapshot

CREATE_AMT_SESSIONS = """
CREATE TABLE IF NOT EXISTS amt_sessions (
    session_id String,
    start_time DateTime64(3, 'UTC'),
    end_time DateTime64(3, 'UTC'),
    payload String
) ENGINE = ReplacingMergeTree(end_time)
ORDER BY (session_id)
"""


class AMTClickHouseRepository:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def initialize(self) -> None:
        await self._client.command(CREATE_AMT_SESSIONS)

    async def upsert(self, snapshot: SessionSnapshot) -> None:
        payload = json.dumps(
            _snapshot_to_dict(snapshot), separators=(",", ":"), default=str
        )
        await self._client.insert(
            "amt_sessions",
            [[snapshot.session_id, snapshot.start, snapshot.end, payload]],
            column_names=["session_id", "start_time", "end_time", "payload"],
        )

    async def get_payload(self, session_id: str) -> dict[str, Any] | None:
        result = await self._client.query(
            "SELECT payload FROM amt_sessions WHERE session_id = {session_id:String} "
            "ORDER BY end_time DESC LIMIT 1",
            parameters={"session_id": session_id},
        )
        if not result.result_rows:
            return None
        return json.loads(result.result_rows[0][0])


def _snapshot_to_dict(snapshot: SessionSnapshot) -> dict[str, Any]:
    context = snapshot.context
    return {
        "session_id": snapshot.session_id,
        "start": snapshot.start.isoformat(),
        "end": snapshot.end.isoformat(),
        "context": {
            "poc": context.poc,
            "vah": context.vah,
            "val": context.val,
            "price_location": context.price_location,
            "acceptance": context.acceptance,
            "rejection": context.rejection,
            "book_imbalance": context.book_imbalance,
            "confidence": context.confidence,
            "state": context.state.value,
            "day_type": context.day_type.value,
            "value_migration": context.value_migration.value,
        },
    }
