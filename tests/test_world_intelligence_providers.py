from datetime import datetime, timezone
import json
from urllib.parse import urlsplit, parse_qs

import pytest

from aitos.world_intelligence.providers import FREDSeriesAdapter, GDELTDocAdapter, SECEdgarSubmissionsAdapter


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_sec_adapter_emits_primary_filings_and_uses_accession_cursor():
    payload = {
        "name": "Example Corp",
        "filings": {"recent": {
            "accessionNumber": ["0000000001-26-000002", "0000000001-26-000001"],
            "form": ["8-K", "10-Q"],
            "filingDate": ["2026-11-13", "2026-11-12"],
            "primaryDocument": ["event.htm", "quarter.htm"],
        }},
    }
    adapter = SECEdgarSubmissionsAdapter(
        cik="1", contact_user_agent="AITOS research contact research@example.com",
        fetcher=lambda url, headers: (json.dumps(payload).encode(), {}),
        min_interval_seconds=0,
    )
    events, cursor = await adapter.poll()
    assert len(events) == 2
    assert events[0].verification == "primary_source"
    assert events[0].source.is_primary
    assert cursor == "0000000001-26-000002"
    events2, cursor2 = await adapter.poll(cursor=cursor)
    assert events2 == []
    assert cursor2 == cursor


@pytest.mark.asyncio
async def test_gdelt_adapter_bounds_records_and_keeps_articles_unverified():
    payload = {"articles": [{
        "title": "Bitcoin exchange reports hack and stolen funds",
        "url": "https://news.example/story?id=private",
        "seendate": "20261113T115900Z",
        "domain": "news.example",
    }]}
    seen = []
    def fetch(url, headers):
        seen.append(url)
        return json.dumps(payload).encode(), {}
    adapter = GDELTDocAdapter(
        query="bitcoin exchange", fetcher=fetch, min_interval_seconds=0,
    )
    events, cursor = await adapter.poll()
    assert len(events) == 1
    assert events[0].verification == "unverified"
    assert "private" not in events[0].source.uri
    assert cursor
    assert parse_qs(urlsplit(seen[0]).query)["maxrecords"] == ["100"]


@pytest.mark.asyncio
async def test_fred_adapter_keeps_api_key_out_of_event_metadata_and_cursor():
    payload = {"observations": [
        {"date": "2026-11-12", "value": "3.1"},
        {"date": "2026-11-13", "value": "3.2"},
    ]}
    seen = []
    def fetch(url, headers):
        seen.append(url)
        return json.dumps(payload).encode(), {}
    adapter = FREDSeriesAdapter(
        series_id="CPIAUCSL", api_key="secret-key", start_date="2026-11-01",
        user_agent="AITOS research contact research@example.com",
        fetcher=fetch, min_interval_seconds=0,
    )
    events, cursor = await adapter.poll()
    assert len(events) == 2
    assert all(event.sentiment == 0.0 for event in events)
    assert "secret-key" not in str(events[0].metadata)
    assert "secret-key" not in str(cursor)
    assert parse_qs(urlsplit(seen[0]).query)["api_key"] == ["secret-key"]


@pytest.mark.asyncio
async def test_fred_rejects_bad_api_response_and_bad_cursor():
    adapter = FREDSeriesAdapter(
        series_id="GDP", api_key="x", start_date="2026-01-01",
        user_agent="AITOS research contact research@example.com",
        fetcher=lambda url, headers: (b"[]", {}), min_interval_seconds=0,
    )
    with pytest.raises(ValueError, match="root"):
        await adapter.poll()
    with pytest.raises(ValueError, match="cursor"):
        await adapter.poll(cursor="not-a-date")


def test_sec_adapter_requires_contact_and_valid_cik():
    with pytest.raises(ValueError, match="contact"):
        SECEdgarSubmissionsAdapter(
            cik="1", contact_user_agent="AITOS", fetcher=lambda url, headers: (b"{}", {}),
        )
