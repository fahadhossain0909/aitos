from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from aitos.capital_intelligence.alerts import CapitalFlowWebhookAlerter


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, key_count, key, token):
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0


class FakeWatchlist:
    def __init__(self, addresses):
        self.addresses = addresses

    async def get(self, chain, address):
        return self.addresses.get(f"{chain}:{address}")


class FakeResponse:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self):
        return None


class FakeSession:
    closed = False

    def __init__(self):
        self.posts = []

    def post(self, url, json):
        self.posts.append((url, json))
        return FakeResponse()


def make_event(**kwargs):
    values = {
        "event_id": "evt-1",
        "event_type": "onchain_transfer",
        "venue": "evm:1",
        "instrument": "USDC",
        "quantity": 100,
        "unit": "token",
        "usd_value": 100,
        "observed_time": datetime.now(timezone.utc),
        "actor_id": "0x" + "a" * 40,
        "counterparty_id": "0x" + "b" * 40,
        "metadata": {"actor_identity_kind": "evm_address"},
        "finality": "confirmed",
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_webhook_alerts_only_watched_entities_and_deduplicates():
    actor = "0x" + "a" * 40
    watchlist = FakeWatchlist({"evm:1:" + actor: {"entity_id": "evm:1:" + actor}})
    session = FakeSession()
    alerter = CapitalFlowWebhookAlerter(
        redis_client=FakeRedis(),
        watchlist=watchlist,
        webhook_url="https://hooks.example.test/cfei",
        session=session,
    )
    event = make_event()
    assert await alerter.notify_event(event) == 1
    assert await alerter.notify_event(event) == 0
    assert len(session.posts) == 1
    assert "not a trading signal" in session.posts[0][1]["text"]


@pytest.mark.asyncio
async def test_webhook_skips_reverted_and_aggregate_events():
    session = FakeSession()
    alerter = CapitalFlowWebhookAlerter(
        redis_client=FakeRedis(),
        watchlist=FakeWatchlist({}),
        webhook_url="https://hooks.example.test/cfei",
        session=session,
    )
    assert await alerter.notify_event(make_event(finality="reverted")) == 0
    assert (
        await alerter.notify_event(make_event(metadata={"aggregate_only": True}))
        == 0
    )
    assert not session.posts


def test_webhook_rejects_non_https_url():
    with pytest.raises(ValueError, match="HTTPS"):
        CapitalFlowWebhookAlerter(
            redis_client=FakeRedis(),
            watchlist=FakeWatchlist({}),
            webhook_url="http://example.test/hook",
        )
