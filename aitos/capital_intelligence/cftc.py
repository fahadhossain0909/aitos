"""Official CFTC public-reporting Commitments of Traders aggregate adapter."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp

from .contracts import CapitalEvent


_FIELD = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_DATASET = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}$")


class CftcCotAdapter:
    """Read CFTC Socrata COT rows with an explicit schema map.

    COT categories are aggregate trader classifications, not individual entities.
    Offset cursors assume the source's report-date/market-code ordering is stable;
    periodic controlled refresh is needed to capture historical corrections.
    """

    BASE_URL = "https://publicreporting.cftc.gov/resource"

    def __init__(
        self,
        *,
        dataset_id: str,
        field_map: dict[str, Any],
        report_type: str = "configured_cot_report",
        market: str = "futures",
        page_limit: int = 1000,
        revision_refresh_pages_per_poll: int = 5,
        timeout_seconds: float = 12.0,
        session: aiohttp.ClientSession | None = None,
    ):
        if not _DATASET.fullmatch(dataset_id):
            raise ValueError("CFTC dataset id must be a Socrata dataset identifier")
        if not 1 <= page_limit <= 5000:
            raise ValueError("CFTC page limit must be in [1, 5000]")
        if (
            isinstance(revision_refresh_pages_per_poll, bool)
            or not 0 <= revision_refresh_pages_per_poll <= 20
        ):
            raise ValueError("CFTC revision refresh pages must be in [0, 20]")
        if not 0.2 <= timeout_seconds <= 30:
            raise ValueError("invalid timeout")
        required = {
            "report_date",
            "market_code",
            "market_name",
            "open_interest",
            "categories",
        }
        if not isinstance(field_map, dict) or not required.issubset(field_map):
            raise ValueError("CFTC field_map is missing required mappings")
        for key in ("report_date", "market_code", "market_name", "open_interest"):
            if not isinstance(field_map[key], str) or not _FIELD.fullmatch(
                field_map[key]
            ):
                raise ValueError(f"invalid CFTC field mapping: {key}")
        categories = field_map["categories"]
        if not isinstance(categories, dict) or not categories or len(categories) > 20:
            raise ValueError("CFTC categories must contain 1-20 position groups")
        normalized_categories = {}
        for name, fields in categories.items():
            if (
                not isinstance(name, str)
                or not _FIELD.fullmatch(name)
                or not isinstance(fields, dict)
                or not {"long", "short"}.issubset(fields)
            ):
                raise ValueError("invalid CFTC category mapping")
            long_field, short_field = fields["long"], fields["short"]
            if not all(
                isinstance(value, str) and _FIELD.fullmatch(value)
                for value in (long_field, short_field)
            ):
                raise ValueError("invalid CFTC long/short field name")
            normalized_categories[name] = {
                "long": long_field,
                "short": short_field,
            }
        self.dataset_id = dataset_id
        self.field_map = {
            **{key: field_map[key] for key in required if key != "categories"},
            "categories": normalized_categories,
        }
        self.report_type = report_type
        self.market = market
        self.page_limit = page_limit
        self.revision_refresh_pages_per_poll = revision_refresh_pages_per_poll
        self.timeout_seconds = timeout_seconds
        self.session = session
        self.owns_session = session is None

    async def close(self) -> None:
        if self.owns_session and self.session and not self.session.closed:
            await self.session.close()

    async def _get_rows(self, offset: int) -> list[dict[str, Any]]:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                headers={"User-Agent": "AITOS-CFEI/1.0"},
            )
        fields = self.field_map
        async with self.session.get(
            f"{self.BASE_URL}/{self.dataset_id}.json",
            params={
                "$limit": str(self.page_limit),
                "$offset": str(offset),
                "$order": f"{fields['report_date']} ASC, {fields['market_code']} ASC",
            },
        ) as response:
            if response.status == 429:
                raise RuntimeError("CFTC public-reporting rate limit reached")
            response.raise_for_status()
            rows = await response.json()
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("CFTC endpoint returned an unexpected row set")
        return rows

    @staticmethod
    def _count(value: Any, *, field: str) -> int:
        try:
            number = Decimal(str(value if value not in (None, "") else "0"))
        except InvalidOperation:
            raise RuntimeError(f"CFTC field {field} is not numeric") from None
        if not number.is_finite() or number < 0 or number != number.to_integral_value():
            raise RuntimeError(f"CFTC field {field} is not a non-negative integer")
        return int(number)

    def _events_from_rows(
        self, rows: list[dict[str, Any]], observed_at: datetime
    ) -> list[CapitalEvent]:
        fields = self.field_map
        events = []
        for row in rows:
            raw_date = str(row.get(fields["report_date"], ""))[:10]
            try:
                report_day = date.fromisoformat(raw_date)
            except ValueError:
                raise RuntimeError("CFTC row has an invalid report date") from None
            market_code = str(row.get(fields["market_code"], "")).strip()
            market_name = str(row.get(fields["market_name"], "")).strip()
            if not market_code:
                raise RuntimeError("CFTC row has no contract market code")
            open_interest = self._count(
                row.get(fields["open_interest"]), field=fields["open_interest"]
            )
            positioning = {}
            for category, mapping in fields["categories"].items():
                long_count = self._count(
                    row.get(mapping["long"]), field=mapping["long"]
                )
                short_count = self._count(
                    row.get(mapping["short"]), field=mapping["short"]
                )
                net = long_count - short_count
                positioning[category] = {
                    "long_contracts": long_count,
                    "short_contracts": short_count,
                    "net_contracts": net,
                    "net_pct_open_interest": (
                        net / open_interest * 100 if open_interest else None
                    ),
                }
            event_id = hashlib.sha256(
                f"{self.dataset_id}|{report_day.isoformat()}|{market_code}".encode()
            ).hexdigest()
            events.append(
                CapitalEvent(
                    event_id,
                    f"cftc-cot:{self.dataset_id}",
                    "cftc_cot_aggregate",
                    self.market,
                    "cftc",
                    market_code,
                    "cot_positioning_report",
                    datetime.combine(report_day, time.min, timezone.utc),
                    observed_at,
                    float(open_interest),
                    "contracts",
                    None,
                    finality="confirmed",
                    confidence=1.0,
                    metadata={
                        "aggregate_only": True,
                        "report_type": self.report_type,
                        "dataset_id": self.dataset_id,
                        "report_date": report_day.isoformat(),
                        "market_code": market_code,
                        "market_name": market_name,
                        "open_interest_contracts": open_interest,
                        "positioning": positioning,
                        "publication_timestamp_not_supplied": True,
                        "source_semantics": (
                            (
                                "weekly_aggregate_positioning_not_individual_trader_activity"
                            )
                        ),
                    },
                )
            )
        return events

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[CapitalEvent], str | None]:
        state = {
            "version": 2,
            "incremental_offset": 0,
            "refresh_offset": 0,
        }
        if cursor is not None:
            try:
                supplied = json.loads(cursor)
            except (TypeError, ValueError):
                raise ValueError("CFTC cursor must be a JSON offset object") from None
            if not isinstance(supplied, dict):
                raise ValueError("invalid CFTC cursor")
            if "offset" in supplied and "incremental_offset" not in supplied:
                supplied = {
                    "version": 2,
                    "incremental_offset": supplied.get("offset"),
                    "refresh_offset": 0,
                }
            if (
                supplied.get("version") != 2
                or any(
                    isinstance(supplied.get(key), bool)
                    or not isinstance(supplied.get(key), int)
                    or not 0 <= supplied[key] <= 10_000_000
                    for key in ("incremental_offset", "refresh_offset")
                )
            ):
                raise ValueError("invalid CFTC version-2 cursor")
            state.update(supplied)

        now = datetime.now(timezone.utc)
        incremental_rows = await self._get_rows(state["incremental_offset"])
        state["incremental_offset"] += len(incremental_rows)

        refreshed_rows: list[dict[str, Any]] = []
        refresh_offset = state["refresh_offset"]
        for _ in range(self.revision_refresh_pages_per_poll):
            page = await self._get_rows(refresh_offset)
            if not page:
                refresh_offset = 0
                break
            refreshed_rows.extend(page)
            refresh_offset += len(page)
            if len(page) < self.page_limit:
                refresh_offset = 0
                break
        state["refresh_offset"] = refresh_offset

        rows_by_key = {}
        for row in [*incremental_rows, *refreshed_rows]:
            key = (
                str(row.get(self.field_map["report_date"], ""))[:10],
                str(row.get(self.field_map["market_code"], "")).strip(),
            )
            rows_by_key[key] = row
        events = self._events_from_rows(list(rows_by_key.values()), now)
        serialized = json.dumps(state, sort_keys=True, separators=(",", ":"))
        return events, serialized
