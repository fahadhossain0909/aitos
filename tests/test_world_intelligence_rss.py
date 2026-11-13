import json
import pytest

from aitos.world_intelligence.rss import RSSFeedAdapter


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Company announces new product</title>
<link>https://news.example.test/story/1</link><guid>story-1</guid>
<pubDate>Fri, 13 Nov 2026 11:00:00 GMT</pubDate></item>
<item><title>Missing timestamp</title><link>https://news.example.test/story/2</link></item>
</channel></rss>"""


@pytest.mark.asyncio
async def test_rss_adapter_emits_unverified_neutral_observations():
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        return RSS, {"etag": '"v1"', "last_modified": "Fri, 13 Nov 2026 11:00:00 GMT"}

    adapter = RSSFeedAdapter(
        source_id="licensed-news",
        feed_url="https://news.example.test/rss",
        allowed_hosts={"news.example.test"},
        fetcher=fetch,
    )
    events, cursor = await adapter.poll()
    assert len(events) == 1
    assert events[0].verification == "unverified"
    assert events[0].sentiment == 0.0
    assert events[0].metadata["sentiment_status"] == "not_analyzed"
    assert events[0].source.original_story_id == "story-1"
    assert json.loads(cursor)["etag"] == '"v1"'
    assert calls[0][1] == {}


@pytest.mark.asyncio
async def test_rss_adapter_uses_conditional_headers_and_handles_not_modified():
    seen = []

    def fetch(url, headers):
        seen.append(headers)
        return None, {"etag": '"v1"'}

    adapter = RSSFeedAdapter(
        source_id="news",
        feed_url="https://news.example.test/rss",
        allowed_hosts={"news.example.test"},
        fetcher=fetch,
    )
    cursor = json.dumps({"etag": '"v1"'})
    events, next_cursor = await adapter.poll(cursor=cursor)
    assert events == []
    assert seen == [{"If-None-Match": '"v1"'}]
    assert json.loads(next_cursor)["etag"] == '"v1"'


def test_rss_adapter_rejects_non_allowlisted_hosts_and_http():
    with pytest.raises(ValueError, match="allowlisted HTTPS"):
        RSSFeedAdapter(
            source_id="bad",
            feed_url="http://news.example.test/rss",
            allowed_hosts={"news.example.test"},
        )
    with pytest.raises(ValueError, match="allowlisted HTTPS"):
        RSSFeedAdapter(
            source_id="bad",
            feed_url="https://attacker.example/rss",
            allowed_hosts={"news.example.test"},
        )


@pytest.mark.asyncio
async def test_rss_adapter_rejects_entity_expansion_payload():
    adapter = RSSFeedAdapter(
        source_id="news",
        feed_url="https://news.example.test/rss",
        allowed_hosts={"news.example.test"},
        fetcher=lambda url, headers: (
            b'<!DOCTYPE x [<!ENTITY e "bad">]><rss><channel/></rss>',
            {},
        ),
    )
    with pytest.raises(ValueError, match="DTD"):
        await adapter.poll()


def test_rss_adapter_rejects_invalid_cursor():
    adapter = RSSFeedAdapter(
        source_id="news",
        feed_url="https://news.example.test/rss",
        allowed_hosts={"news.example.test"},
        fetcher=lambda url, headers: (RSS, {}),
    )
    with pytest.raises(ValueError, match="cursor"):
        import asyncio

        asyncio.run(adapter.poll(cursor='{"unexpected": "value"}'))


@pytest.mark.asyncio
async def test_rss_adapter_refuses_to_checkpoint_truncated_feed():
    oversized = (
        b"<rss><channel>"
        b"<item><title>A</title><pubDate>Fri, 13 Nov 2026 11:00:00 GMT</pubDate></item>"
        b"<item><title>B</title><pubDate>Fri, 13 Nov 2026 10:00:00 GMT</pubDate></item>"
        b"</channel></rss>"
    )
    adapter = RSSFeedAdapter(
        source_id="news",
        feed_url="https://news.example.test/rss",
        allowed_hosts={"news.example.test"},
        max_items=1,
        fetcher=lambda url, headers: (oversized, {"etag": '"v2"'}),
    )
    with pytest.raises(ValueError, match="exceeds max_items"):
        await adapter.poll()


@pytest.mark.asyncio
async def test_rss_adapter_redacts_query_parameters_from_stored_uris():
    body = b"""<rss><channel><item>
    <title>Public headline</title>
    <link>https://news.example.test/story/1?access_token=secret</link>
    <pubDate>Fri, 13 Nov 2026 11:00:00 GMT</pubDate>
    </item></channel></rss>"""
    adapter = RSSFeedAdapter(
        source_id="news",
        feed_url="https://news.example.test/rss?api_key=secret",
        allowed_hosts={"news.example.test"},
        fetcher=lambda url, headers: (body, {}),
    )
    events, _ = await adapter.poll()
    assert len(events) == 1
    assert "secret" not in events[0].source.uri
    assert "secret" not in events[0].metadata["feed_url"]
