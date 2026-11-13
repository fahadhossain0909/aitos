"""Bounded, allowlisted RSS/Atom ingestion for public event feeds.

Feed items are deliberately emitted as unverified, neutral observations. A
separate verification/NLP pipeline must establish facts and sentiment before
they can influence directional impact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence


FetchResult = tuple[bytes | None, Mapping[str, str]]


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_feed_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_feed_url(url: str, allowed_hosts: frozenset[str]) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or host not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("feed URL and redirects must use an allowlisted HTTPS host")
    return host


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, names: set[str]) -> str | None:
    for child in element:
        if _local_name(child.tag) in names:
            href = child.attrib.get("href")
            value = href or child.text
            if value and value.strip():
                return value.strip()
    return None


def _published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


class RSSFeedAdapter:
    """Fetch one bounded RSS/Atom document with conditional HTTP caching."""

    def __init__(
        self,
        *,
        source_id: str,
        feed_url: str,
        allowed_hosts: set[str] | frozenset[str],
        reliability: float = 0.5,
        max_items: int = 100,
        max_response_bytes: int = 1_000_000,
        timeout_seconds: float = 10.0,
        fetcher: Callable[[str, Mapping[str, str]], FetchResult] | None = None,
    ) -> None:
        self.source_id = source_id.strip()
        if not self.source_id:
            raise ValueError("source_id is required")
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        if not self.allowed_hosts:
            raise ValueError("at least one allowed feed host is required")
        _validate_feed_url(feed_url, self.allowed_hosts)
        if not 1 <= max_items <= 500:
            raise ValueError("max_items must be between 1 and 500")
        if not 1_024 <= max_response_bytes <= 4_000_000:
            raise ValueError("max_response_bytes must be between 1024 and 4000000")
        if not 0.0 <= float(reliability) <= 1.0:
            raise ValueError("reliability must be between 0 and 1")
        if not 0.1 <= float(timeout_seconds) <= 30.0:
            raise ValueError("timeout_seconds must be between 0.1 and 30")
        self.feed_url = feed_url
        self.reliability = float(reliability)
        self.max_items = max_items
        self.max_response_bytes = max_response_bytes
        self.timeout_seconds = float(timeout_seconds)
        self.fetcher = fetcher

    def _fetch(self, url: str, headers: Mapping[str, str]) -> FetchResult:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "AITOS-World-Intelligence/1.0",
                "Accept": (
                    "application/atom+xml, application/rss+xml, application/xml, text/xml"
                ),
                **dict(headers),
            },
        )
        opener = urllib.request.build_opener(
            _AllowlistedRedirectHandler(self.allowed_hosts)
        )
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                _validate_feed_url(response.geturl(), self.allowed_hosts)
                body = response.read(self.max_response_bytes + 1)
                if len(body) > self.max_response_bytes:
                    raise ValueError("feed response exceeded configured byte limit")
                return body, {
                    "etag": response.headers.get("ETag", ""),
                    "last_modified": response.headers.get("Last-Modified", ""),
                }
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return None, {
                    "etag": exc.headers.get("ETag", ""),
                    "last_modified": exc.headers.get("Last-Modified", ""),
                }
            raise

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[IntelligenceEvent], str | None]:
        cache: dict[str, str] = {}
        if cursor:
            try:
                decoded = json.loads(cursor)
                if not isinstance(decoded, dict) or any(
                    key not in {"etag", "last_modified"} or not isinstance(value, str)
                    for key, value in decoded.items()
                ):
                    raise ValueError
                cache = decoded
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("invalid RSS conditional-request cursor") from exc
        request_headers = {}
        if cache.get("etag"):
            request_headers["If-None-Match"] = cache["etag"]
        if cache.get("last_modified"):
            request_headers["If-Modified-Since"] = cache["last_modified"]

        fetch = self.fetcher or self._fetch
        body, response_headers = await asyncio.to_thread(
            fetch, self.feed_url, request_headers
        )
        next_cursor = json.dumps(
            {
                key: value
                for key, value in {
                    "etag": response_headers.get("etag", ""),
                    "last_modified": response_headers.get("last_modified", ""),
                }.items()
                if value
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if body is None:
            return [], next_cursor or cursor
        if re.search(rb"<!\s*(DOCTYPE|ENTITY)", body, re.IGNORECASE):
            raise ValueError("DTD and entity declarations are not accepted in feeds")
        root = ET.fromstring(body)
        now = datetime.now(timezone.utc)
        response_hash = hashlib.sha256(body).hexdigest()
        events: list[IntelligenceEvent] = []
        entries = [
            item for item in root.iter() if _local_name(item.tag) in {"item", "entry"}
        ]
        if len(entries) > self.max_items:
            raise ValueError(
                "feed item count exceeds max_items; increase the configured limit"
            )
        for item in entries:
            title = _child_text(item, {"title"})
            link = _child_text(item, {"link"})
            guid = _child_text(item, {"guid", "id"})
            published = _published_at(
                _child_text(item, {"pubdate", "published", "updated", "date"})
            )
            if not title or published is None or published > now:
                continue
            story_uri = urljoin(self.feed_url, link) if link else self.feed_url
            origin_id = guid or story_uri
            event_id = hashlib.sha256(
                f"{self.source_id}|{origin_id}".encode("utf-8")
            ).hexdigest()
            source = SourceEvidence(
                source_id=self.source_id,
                source_kind="news_feed",
                uri=story_uri,
                published_at=published,
                first_seen_at=max(now, published),
                reliability=self.reliability,
                is_primary=False,
                original_story_id=origin_id,
            )
            events.append(
                IntelligenceEvent(
                    event_id=event_id,
                    event_type="news",
                    title=title[:500],
                    entities=(),
                    published_at=published,
                    first_seen_at=max(now, published),
                    source=source,
                    sentiment=0.0,
                    severity=0.1,
                    novelty=0.5,
                    confidence=0.1,
                    verification="unverified",
                    impact_horizon="unknown",
                    metadata={
                        "feed_url": self.feed_url,
                        "story_uri": story_uri,
                        "response_sha256": response_hash,
                        "sentiment_status": "not_analyzed",
                    },
                )
            )
        return events, next_cursor or cursor


__all__ = ["RSSFeedAdapter"]
