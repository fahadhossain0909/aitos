"""OAuth helpers for cTrader Open API.

The cTrader Open API uses OAuth 2.0 for account authorization. Tokens should
be kept outside source control and injected from the AITOS secret manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp


@dataclass(frozen=True)
class CTraderToken:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    token_type: str = "Bearer"


class CTraderOAuthClient:
    AUTH_URL = "https://id.ctrader.com/my/settings/openapi-granting-access/"
    TOKEN_URL = "https://openapi.ctrader.com/apps/token"

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def authorization_url(self, *, scope: str = "trading") -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": scope,
            }
        )
        return f"{self.AUTH_URL}?{query}"

    async def exchange_code(self, code: str) -> CTraderToken:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str) -> CTraderToken:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def _token_request(self, payload: dict[str, str]) -> CTraderToken:
        payload.update({"client_id": self.client_id, "client_secret": self.client_secret})
        async with aiohttp.ClientSession() as session:
            async with session.get(self.TOKEN_URL, params=payload) as response:
                body = await response.json(content_type=None)
                if response.status >= 400 or "accessToken" not in body:
                    raise RuntimeError(f"cTrader OAuth error {response.status}: {body}")
                return CTraderToken(
                    access_token=body["accessToken"],
                    refresh_token=body.get("refreshToken"),
                    expires_in=body.get("expiresIn"),
                    token_type=body.get("tokenType", "Bearer"),
                )
