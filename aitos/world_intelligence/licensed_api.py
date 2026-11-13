"""Operator-mapped adapter for licensed news and social JSON APIs.

This deliberately has no vendor-specific scraping or auth assumptions. The
operator supplies an HTTPS allowlist, response field map and secret headers
from environment variables after reviewing the provider's licence and limits.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence
from aitos.world_intelligence.financial_nlp import analyze_financial_headline


class _AllowedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_endpoint(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_endpoint(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("licensed API endpoint must use an allowlisted HTTPS host")


def _get_path(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for component in path.split("."):
        if not isinstance(current, Mapping) or component not in current:
            raise ValueError(f"licensed API records path {path!r} is missing")
        current = current[component]
    return current


class LicensedJSONFeedAdapter:
    """Normalize one bounded page from an operator-approved JSON API."""

    def __init__(
        self,
        *,
        source_id: str,
        endpoint: str,
        allowed_hosts: set[str] | frozenset[str],
        records_path: str,
        field_map: Mapping[str, str],
        query_params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        cursor_param: str | None = None,
        source_kind: str = "licensed_news_or_social",
        reliability: float = 0.4,
        max_records: int = 100,
        max_response_bytes: int = 2_000_000,
        timeout_seconds: float = 10.0,
        min_interval_seconds: float = 1.0,
        overlap_seconds: float = 60.0,
        fetcher: Callable[[str, Mapping[str, str]], bytes] | None = None,
    ):
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        if not self.allowed_hosts:
            raise ValueError("at least one licensed API host must be allowlisted")
        _validate_endpoint(endpoint, self.allowed_hosts)
        sensitive_keys = {"api_key", "apikey", "token", "access_token", "auth", "key"}
        endpoint_keys = {
            key.casefold()
            for key, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(endpoint).query)
        }
        if endpoint_keys & sensitive_keys:
            raise ValueError("licensed API secrets must be supplied in headers, not URLs")
        if not source_id.strip() or not records_path.strip():
            raise ValueError("source_id and records_path are required")
        required = {"title", "published_at", "uri"}
        if not required.issubset(field_map):
            raise ValueError("field_map must define title, published_at and uri")
        if not 1 <= max_records <= 500 or not 1024 <= max_response_bytes <= 8_000_000:
            raise ValueError("licensed API response limits are invalid")
        if not 0.1 <= timeout_seconds <= 30 or min_interval_seconds < 0:
            raise ValueError("licensed API timeout/rate limit is invalid")
        if overlap_seconds < 0 or not 0 <= reliability <= 1:
            raise ValueError("licensed API overlap/reliability is invalid")
        self.source_id = source_id.strip()
        self.endpoint = endpoint
        self.records_path = records_path
        self.field_map = dict(field_map)
        self.query_params = dict(query_params or {})
        if {key.casefold() for key in self.query_params} & sensitive_keys:
            raise ValueError("licensed API secrets must be supplied in headers, not query parameters")
        self.headers = dict(headers or {})
        self.cursor_param = cursor_param
        self.source_kind = source_kind
        self.reliability = float(reliability)
        self.max_records = max_records
        self.max_response_bytes = max_response_bytes
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = min_interval_seconds
        self.overlap_seconds = overlap_seconds
        self.fetcher = fetcher
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    def _fetch(self, url: str, headers: Mapping[str, str]) -> bytes:
        _validate_endpoint(url, self.allowed_hosts)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "AITOS-World-Intelligence/1.0", **dict(headers)},
        )
        opener = urllib.request.build_opener(_AllowedRedirect(self.allowed_hosts))
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                _validate_endpoint(response.geturl(), self.allowed_hosts)
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"licensed API HTTP status {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("licensed API request failed") from None
        if len(body) > self.max_response_bytes:
            raise ValueError("licensed API response exceeded byte limit")
        return body

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[IntelligenceEvent], str | None]:
        now = datetime.now(timezone.utc)
        params = dict(self.query_params)
        if cursor:
            try:
                parsed_cursor = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("invalid licensed API cursor") from exc
            if parsed_cursor.tzinfo is None:
                raise ValueError("licensed API cursor must be timezone-aware")
            if self.cursor_param:
                params[self.cursor_param] = (
                    parsed_cursor.astimezone(timezone.utc)
                    - timedelta(seconds=self.overlap_seconds)
                ).isoformat()
        url = self.endpoint
        if params:
            separator = "&" if urllib.parse.urlsplit(url).query else "?"
            url += separator + urllib.parse.urlencode(params)
        _validate_endpoint(url, self.allowed_hosts)
        async with self._lock:
            delay = self.min_interval_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            fetch = self.fetcher or self._fetch
            try:
                body = await asyncio.to_thread(fetch, url, self.headers)
            finally:
                self._last_request = time.monotonic()
        try:
            payload = json.loads(body)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("licensed API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("licensed API JSON root must be an object")
        records = _get_path(payload, self.records_path)
        if not isinstance(records, list) or len(records) > self.max_records:
            raise ValueError("licensed API record list exceeds configured limit")
        if len(records) == self.max_records:
            raise ValueError(
                "licensed API page reached max_records; refusing cursor advance"
            )

        events: list[IntelligenceEvent] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue

            def field(name: str, default: Any = None) -> Any:
                path = self.field_map.get(name)
                return _get_path(record, path) if path else default

            title = field("title")
            published_raw = field("published_at")
            uri = field("uri")
            if not isinstance(title, str) or not title.strip():
                continue
            if not isinstance(uri, str):
                continue
            try:
                published = datetime.fromisoformat(
                    str(published_raw).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                continue
            if published.tzinfo is None:
                continue
            published = published.astimezone(timezone.utc)
            if published > now:
                continue
            parsed_uri = urllib.parse.urlsplit(uri)
            if (
                parsed_uri.scheme not in {"http", "https"}
                or not parsed_uri.hostname
                or parsed_uri.username is not None
                or parsed_uri.password is not None
            ):
                continue
            safe_uri = parsed_uri._replace(query="", fragment="").geturl()
            origin_id = str(field("id") or safe_uri)
            digest = hashlib.sha256(
                f"{self.source_id}|{origin_id}".encode("utf-8")
            ).hexdigest()
            analysis = analyze_financial_headline(title[:20_000])
            raw_entities = field("entities", [])
            entities = (
                tuple(str(item).upper() for item in raw_entities[:32])
                if isinstance(raw_entities, list)
                else ()
            )
            source = SourceEvidence(
                source_id=self.source_id,
                source_kind=self.source_kind,
                uri=safe_uri,
                published_at=published,
                first_seen_at=now,
                reliability=self.reliability,
                is_primary=False,
                original_story_id=digest,
            )
            events.append(
                IntelligenceEvent(
                    event_id=digest,
                    event_type=str(field("event_type") or analysis.event_type)[:64],
                    title=title[:500],
                    entities=entities,
                    published_at=published,
                    first_seen_at=now,
                    source=source,
                    sentiment=analysis.sentiment,
                    severity=analysis.severity,
                    novelty=0.5,
                    confidence=analysis.confidence,
                    verification="unverified",
                    impact_horizon=analysis.impact_horizon,
                    metadata={
                        "provider": "licensed_json",
                        "sentiment_status": "heuristic_unverified",
                    },
                )
            )
        return events, now.isoformat()


__all__ = ["LicensedJSONFeedAdapter"]
