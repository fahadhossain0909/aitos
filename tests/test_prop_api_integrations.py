from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aitos.capital.models import AccountSnapshot, PropFirmProfile, PropRuleSet
from aitos.execution.ctrader_auth import CTraderOAuthClient
from aitos.execution.mt5_gateway import MT5GatewayOrderExecutor
from aitos.execution.order_executor import OrderRequest
from aitos.models.trade import TradeSide


def test_ctrader_authorization_url_uses_official_endpoint():
    client = CTraderOAuthClient("id", "secret", "https://localhost/callback")
    url = client.authorization_url()
    assert url.startswith("https://openapi.ctrader.com/apps/auth?")
    assert "client_id=id" in url
    assert "scope=trading" in url


def test_prop_account_snapshot_tracks_loss_and_drawdown():
    account = AccountSnapshot(
        account_id="demo-1",
        provider="test",
        equity=96_000,
        balance=97_000,
        day_start_equity=100_000,
        high_water_mark=102_000,
    )
    assert account.daily_loss == 4_000
    assert account.drawdown == 6_000


def test_prop_rules_can_disable_automation():
    profile = PropFirmProfile(
        provider="test",
        platform="api",
        rules=PropRuleSet(automation_allowed=False),
    )
    assert profile.rules.automation_allowed is False


@pytest.mark.asyncio
async def test_mt5_gateway_posts_provider_neutral_order():
    response = MagicMock()
    response.status = 200
    response.json = AsyncMock(
        return_value={
            "order_id": "123",
            "filled_quantity": 0.1,
            "fill_price": 1.25,
            "success": True,
        }
    )
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.post.return_value = response
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=session)

    executor = MT5GatewayOrderExecutor("http://gateway", "secret", session_factory=factory)
    result = await executor.submit_order(
        OrderRequest("EURUSD", TradeSide.LONG, 0.1, 1.25)
    )
    assert result.order_id == "123"
    assert result.filled_quantity == 0.1
    session.post.assert_called_once()
