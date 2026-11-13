"""Bounded official-source adapters for GDELT, SEC EDGAR and FRED.

No credentials are bundled. Each adapter uses a fixed HTTPS host, bounded
responses, an operator-configured User-Agent/API key and replayable cursor.
Provider terms and rate limits remain the operator's responsibility.
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
from datetime import date, datetime, timedelta, timezone
from typing import Any

from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence
from aitos.world_intelligence.financial_nlp import analyze_financial_headline

JSONFetchResult = tuple[bytes, Mapping[str, str]]


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    def __init__(self, host: str):
        super().__init__()
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl, self.host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_url(url: str, host: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("provider URL must use its fixed HTTPS host")


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


class _JSONSource:
    host = ""

    def __init__(
        self,
        *,
        source_id: str,
        user_agent: str,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 2_000_000,
        min_interval_seconds: float = 1.0,
        fetcher: Callable[[str, Mapping[str, str]], JSONFetchResult] | None = None,
    ):
        if not source_id.strip() or not user_agent.strip():
            raise ValueError("source_id and descriptive User-Agent are required")
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 0.1 and 30")
        if not 1024 <= max_response_bytes <= 8_000_000:
            raise ValueError("max_response_bytes must be between 1024 and 8000000")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative")
        self.source_id = source_id.strip()
        self.user_agent = user_agent.strip()
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self.min_interval_seconds = float(min_interval_seconds)
        self.fetcher = fetcher
        self._last_request = 0.0
        self._rate_lock = asyncio.Lock()

    def _fetch(self, url: str, headers: Mapping[str, str]) -> JSONFetchResult:
        _validate_url(url, self.host)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                **dict(headers),
            },
        )
        opener = urllib.request.build_opener(_RedirectGuard(self.host))
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                _validate_url(response.geturl(), self.host)
                body = response.read(self.max_response_bytes + 1)
                if len(body) > self.max_response_bytes:
                    raise ValueError("provider response exceeded configured byte limit")
                return body, dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"provider HTTP status {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("provider request failed") from None

    async def _get_json(self, url: str) -> Mapping[str, Any]:
        _validate_url(url, self.host)
        async with self._rate_lock:
            delay = self.min_interval_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            fetch = self.fetcher or self._fetch
            try:
                body, _ = await asyncio.to_thread(fetch, url, {})
            finally:
                self._last_request = time.monotonic()
        try:
            payload = json.loads(body)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("provider JSON root must be an object")
        return payload


class GDELTDocAdapter(_JSONSource):
    """GDELT DOC 2.0 article search; articles remain unverified observations."""

    host = "api.gdeltproject.org"

    def __init__(
        self,
        *,
        query: str,
        source_id: str = "gdelt-doc",
        lookback_minutes: int = 15,
        max_records: int = 100,
        user_agent: str = "AITOS-World-Intelligence/1.0 operator-configured",
        **kwargs,
    ):
        if (
            not query.strip()
            or not 1 <= lookback_minutes <= 1440
            or not 1 <= max_records <= 250
        ):
            raise ValueError("invalid GDELT query/window/record limit")
        self.query = query.strip()
        self.lookback_minutes = lookback_minutes
        self.max_records = max_records
        super().__init__(source_id=source_id, user_agent=user_agent, **kwargs)

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[IntelligenceEvent], str | None]:
        now = datetime.now(timezone.utc)
        prior = (
            _parse_utc(cursor)
            if cursor
            else now - timedelta(minutes=self.lookback_minutes)
        )
        if cursor and prior is None:
            raise ValueError("invalid GDELT cursor")
        start = max(prior - timedelta(minutes=5), now - timedelta(days=1))
        params = {
            "query": self.query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": str(self.max_records),
            "sort": "HybridRel",
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": now.strftime("%Y%m%d%H%M%S"),
        }
        url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(
            params
        )
        payload = await self._get_json(url)
        articles = payload.get("articles", [])
        if not isinstance(articles, list) or len(articles) >= self.max_records:
            raise ValueError(
                "GDELT page reached max_records; refusing cursor advance"
            )
        events: list[IntelligenceEvent] = []
        for article in articles:
            if not isinstance(article, dict):
                continue
            title = article.get("title")
            uri = article.get("url")
            raw_time = str(article.get("seendate", ""))
            try:
                published = datetime.strptime(raw_time, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            if (
                not isinstance(title, str)
                or not title.strip()
                or not isinstance(uri, str)
                or published > now
            ):
                continue
            parsed = urllib.parse.urlsplit(uri)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                continue
            safe_uri = parsed._replace(query="", fragment="").geturl()
            digest = hashlib.sha256(safe_uri.encode("utf-8")).hexdigest()
            analysis = analyze_financial_headline(title[:20_000])
            source = SourceEvidence(
                source_id=self.source_id,
                source_kind="gdelt_article_index",
                uri=safe_uri,
                published_at=published,
                first_seen_at=now,
                reliability=0.35,
                is_primary=False,
                original_story_id=digest,
            )
            events.append(
                IntelligenceEvent(
                    event_id=digest,
                    event_type=analysis.event_type,
                    title=title[:500],
                    entities=(),
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
                        "provider": "gdelt_doc",
                        "source_domain": str(article.get("domain", ""))[:200],
                        "sentiment_status": "heuristic_unverified",
                    },
                )
            )
        return events, now.isoformat()


class SECEdgarSubmissionsAdapter(_JSONSource):
    """Official SEC submissions feed for one CIK; filing metadata is primary."""

    host = "data.sec.gov"

    def __init__(
        self,
        *,
        cik: str,
        contact_user_agent: str,
        source_id: str | None = None,
        max_filings: int = 1000,
        **kwargs,
    ):
        if not contact_user_agent.strip() or "@" not in contact_user_agent:
            raise ValueError("SEC User-Agent must identify an operator contact email")
        normalized_cik = cik.strip().lstrip("0")
        if not normalized_cik.isdigit() or len(normalized_cik) > 10:
            raise ValueError("CIK must contain up to 10 digits")
        if not 1 <= max_filings <= 1000:
            raise ValueError("max_filings must be between 1 and 1000")
        self.cik = normalized_cik.zfill(10)
        self.max_filings = max_filings
        super().__init__(
            source_id=source_id or f"sec-cik-{self.cik}",
            user_agent=contact_user_agent,
            **kwargs,
        )

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[IntelligenceEvent], str | None]:
        url = f"https://data.sec.gov/submissions/CIK{self.cik}.json"
        payload = await self._get_json(url)
        company = str(payload.get("name", "SEC registrant"))[:200]
        filings = payload.get("filings", {})
        recent = filings.get("recent", {}) if isinstance(filings, dict) else {}
        accessions = recent.get("accessionNumber", [])
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        documents = recent.get("primaryDocument", [])
        arrays = (accessions, forms, dates, documents)
        if not all(isinstance(values, list) for values in arrays):
            raise ValueError(
                "SEC submissions response has invalid recent filing arrays"
            )
        if len({len(values) for values in arrays}) != 1:
            raise ValueError("SEC recent filing arrays have inconsistent lengths")
        if len(accessions) > self.max_filings:
            raise ValueError(
                "SEC recent filings exceed max_filings; increase limit before checkpoint"
            )
        count = len(accessions)
        now = datetime.now(timezone.utc)
        next_cursor = str(accessions[0]) if count else cursor
        events: list[IntelligenceEvent] = []
        for index in range(count):
            accession = str(accessions[index])
            if cursor and accession == cursor:
                break
            try:
                filed = date.fromisoformat(str(dates[index]))
            except ValueError:
                continue
            published = datetime.combine(filed, datetime.min.time(), timezone.utc)
            if published > now:
                continue
            form = str(forms[index])[:32]
            document = str(documents[index])
            if not document or "/" in document or "\\" in document or ".." in document:
                continue
            accession_path = accession.replace("-", "")
            uri = (
                f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/"
                f"{accession_path}/{urllib.parse.quote(document)}"
            )
            event_id = hashlib.sha256(
                f"{self.cik}|{accession}".encode("utf-8")
            ).hexdigest()
            title = f"{company} filed SEC form {form}"
            source = SourceEvidence(
                source_id=self.source_id,
                source_kind="sec_official_filing",
                uri=uri,
                published_at=published,
                first_seen_at=now,
                reliability=0.99,
                is_primary=True,
                original_story_id=accession,
            )
            events.append(
                IntelligenceEvent(
                    event_id=event_id,
                    event_type="sec_filing",
                    title=title,
                    entities=(f"CIK:{self.cik}",),
                    published_at=published,
                    first_seen_at=now,
                    source=source,
                    sentiment=0.0,
                    severity=0.2,
                    novelty=0.3,
                    confidence=0.5,
                    verification="primary_source",
                    impact_horizon="multi_day",
                    metadata={
                        "provider": "sec_edgar",
                        "form": form,
                        "company": company,
                    },
                )
            )
        return events, next_cursor


class FREDSeriesAdapter(_JSONSource):
    """Official FRED observations; macro values are not directional news."""

    host = "api.stlouisfed.org"

    def __init__(
        self,
        *,
        series_id: str,
        api_key: str,
        start_date: str,
        source_id: str | None = None,
        max_observations: int = 1000,
        **kwargs,
    ):
        if not series_id.strip() or not api_key.strip():
            raise ValueError("FRED series_id and API key are required")
        try:
            date.fromisoformat(start_date)
        except ValueError as exc:
            raise ValueError("start_date must use YYYY-MM-DD") from exc
        if not 1 <= max_observations <= 1000:
            raise ValueError("max_observations must be between 1 and 1000")
        self.series_id = series_id.strip().upper()
        self._api_key = api_key.strip()
        self.start_date = start_date
        self.max_observations = max_observations
        super().__init__(
            source_id=source_id or f"fred-{self.series_id.lower()}",
            **kwargs,
        )

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[IntelligenceEvent], str | None]:
        if cursor:
            try:
                date.fromisoformat(cursor)
            except ValueError as exc:
                raise ValueError("invalid FRED observation cursor") from exc
        params = {
            "series_id": self.series_id,
            "api_key": self._api_key,
            "file_type": "json",
            "observation_start": cursor or self.start_date,
            "sort_order": "asc",
            "limit": str(self.max_observations),
        }
        url = (
            "https://api.stlouisfed.org/fred/series/observations?"
            + urllib.parse.urlencode(params)
        )
        payload = await self._get_json(url)
        observations = payload.get("observations", [])
        if (
            not isinstance(observations, list)
            or len(observations) > self.max_observations
        ):
            raise ValueError("FRED observations are invalid or exceed configured limit")
        now = datetime.now(timezone.utc)
        events: list[IntelligenceEvent] = []
        next_cursor = cursor
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            observation_date = str(observation.get("date", ""))
            try:
                day = date.fromisoformat(observation_date)
            except (ValueError, TypeError):
                continue
            if cursor and observation_date <= cursor:
                continue
            if day > now.date():
                continue
            next_cursor = observation_date
            try:
                value = float(observation.get("value"))
            except (ValueError, TypeError):
                continue
            published = datetime.combine(day, datetime.min.time(), timezone.utc)
            digest = hashlib.sha256(
                f"{self.series_id}|{observation_date}|{value}".encode("utf-8")
            ).hexdigest()
            uri = f"https://fred.stlouisfed.org/series/{urllib.parse.quote(self.series_id)}"
            source = SourceEvidence(
                source_id=self.source_id,
                source_kind="fred_official_macro_data",
                uri=uri,
                published_at=published,
                first_seen_at=now,
                reliability=0.99,
                is_primary=True,
                original_story_id=f"{self.series_id}:{observation_date}",
            )
            events.append(
                IntelligenceEvent(
                    event_id=digest,
                    event_type="macro_data",
                    title=f"FRED {self.series_id} observation for {observation_date}",
                    entities=(self.series_id,),
                    published_at=published,
                    first_seen_at=now,
                    source=source,
                    sentiment=0.0,
                    severity=0.1,
                    novelty=0.2,
                    confidence=0.5,
                    verification="primary_source",
                    impact_horizon="multi_day",
                    metadata={
                        "provider": "fred",
                        "series_id": self.series_id,
                        "observation_date": observation_date,
                        "value": value,
                    },
                )
            )
        return events, next_cursor or cursor


__all__ = ["GDELTDocAdapter", "SECEdgarSubmissionsAdapter", "FREDSeriesAdapter"]
