"""Explicit token metadata registry; no guessed decimals or symbol inference."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    chain_id: str
    address: str
    asset_id: str
    symbol: str
    decimals: int
    price_symbol: str | None = None
    price_quote_currency: str = "USDT"

    def __post_init__(self):
        if not all((self.chain_id.strip(), self.address.strip(), self.asset_id.strip(), self.symbol.strip())):
            raise ValueError("token identity fields are required")
        if not 0 <= self.decimals <= 36:
            raise ValueError("token decimals must be in [0, 36]")
        if self.price_symbol is not None and (
            not self.price_symbol.isascii()
            or not self.price_symbol.isalnum()
            or self.price_symbol.upper() != self.price_symbol
        ):
            raise ValueError("price symbol must be uppercase alphanumeric")
        if not self.price_quote_currency.isascii() or not self.price_quote_currency.isalnum():
            raise ValueError("invalid price quote currency")

    def normalize_raw(self, raw_amount: str | int) -> Decimal:
        try:
            amount = Decimal(str(raw_amount))
        except InvalidOperation:
            raise ValueError("raw token amount must be an integer") from None
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise ValueError("raw token amount must be a finite integer")
        return amount.scaleb(-self.decimals)


class TokenRegistry:
    def __init__(self, tokens: Iterable[TokenMetadata]):
        self._tokens: dict[tuple[str, str], TokenMetadata] = {}
        for token in tokens:
            key = (token.chain_id.lower(), token.address.lower())
            if key in self._tokens:
                raise ValueError(f"duplicate token registry entry: {key[0]}:{key[1]}")
            self._tokens[key] = token

    def get(self, chain_id: str, address: str) -> TokenMetadata | None:
        return self._tokens.get((chain_id.lower(), address.lower()))
