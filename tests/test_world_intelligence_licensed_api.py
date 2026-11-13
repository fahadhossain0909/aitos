import json
from urllib.parse import parse_qs, urlsplit

import pytest

from aitos.world_intelligence.licensed_api import LicensedJSONFeedAdapter


PAYLOAD = {
    "data": {
        "items": [
            {
                "headline": "Exchange reports hack and stolen funds",
                "created": "2026-11-13T11:59:00Z",
                "permalink": "https://social.example/posts/1?token=private",
                "post_id": "post-1",
                "symbols": ["BTC"],
            }
        ]
    }
}


@pytest.mark.asyncio
async def test_licensed_json_adapter_normalizes_social_records_and_redacts_urls():
    seen = []

    def fetch(url, headers):
        seen.append((url, headers))
        return json.dumps(PAYLOAD).encode()

    adapter = LicensedJSONFeedAdapter(
        source_id="licensed-social",
        endpoint="https://api.social.example/v1/search",
        allowed_hosts={"api.social.example"},
        records_path="data.items",
        field_map={
            "title": "headline",
            "published_at": "created",
            "uri": "permalink",
            "id": "post_id",
            "entities": "symbols",
        },
        headers={"Authorization": "Bearer secret"},
        query_params={"q": "bitcoin"},
        cursor_param="since",
        fetcher=fetch,
        min_interval_seconds=0,
    )
    events, cursor = await adapter.poll()
    assert len(events) == 1
    assert events[0].entities == ("BTC",)
    assert events[0].verification == "unverified"
    assert "private" not in events[0].source.uri
    assert "secret" not in events[0].to_json()
    assert parse_qs(urlsplit(seen[0][0]).query)["q"] == ["bitcoin"]
    assert seen[0][1]["Authorization"] == "Bearer secret"
    assert cursor


@pytest.mark.asyncio
async def test_licensed_json_adapter_rejects_non_allowlisted_and_full_pages():
    with pytest.raises(ValueError, match="allowlisted HTTPS"):
        LicensedJSONFeedAdapter(
            source_id="bad",
            endpoint="http://api.social.example/feed",
            allowed_hosts={"api.social.example"},
            records_path="data.items",
            field_map={
                "title": "headline",
                "published_at": "created",
                "uri": "permalink",
            },
        )
    adapter = LicensedJSONFeedAdapter(
        source_id="licensed",
        endpoint="https://api.social.example/feed",
        allowed_hosts={"api.social.example"},
        records_path="data.items",
        field_map={"title": "headline", "published_at": "created", "uri": "permalink"},
        max_records=1,
        fetcher=lambda url, headers: json.dumps(PAYLOAD).encode(),
        min_interval_seconds=0,
    )
    with pytest.raises(ValueError, match="refusing cursor advance"):
        await adapter.poll()
