"""Deterministic, operator-extensible entity resolution for financial headlines.

No fuzzy model is used: aliases are matched on token boundaries and longest
matches win. Ambiguous aliases should not be registered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    canonical_id: str
    matched_alias: str


class EntityAliasRegistry:
    def __init__(
        self, aliases: Mapping[str, tuple[str, ...] | list[str]] | None = None
    ):
        self._aliases: dict[str, str] = {}
        for canonical, values in (aliases or {}).items():
            self.register(canonical, values)

    def register(self, canonical_id: str, aliases: tuple[str, ...] | list[str]) -> None:
        canonical = canonical_id.strip().upper()
        if not canonical or not aliases:
            raise ValueError("canonical entity and aliases are required")
        for alias in (canonical_id, *aliases):
            normalized = " ".join(alias.casefold().split())
            if not normalized:
                continue
            existing = self._aliases.get(normalized)
            if existing is not None and existing != canonical:
                raise ValueError(f"alias {alias!r} is already assigned to {existing}")
            self._aliases[normalized] = canonical

    def resolve_text(
        self, text: str, *, max_entities: int = 32
    ) -> tuple[ResolvedEntity, ...]:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not 1 <= max_entities <= 256:
            raise ValueError("max_entities must be between 1 and 256")
        normalized = " ".join(text.casefold().split())
        matches: list[tuple[int, int, str, str]] = []
        for alias, canonical in self._aliases.items():
            pattern = re.compile(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", re.I)
            for match in pattern.finditer(normalized):
                matches.append((match.start(), match.end(), alias, canonical))
        matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
        accepted: list[ResolvedEntity] = []
        occupied: list[tuple[int, int]] = []
        seen: set[str] = set()
        for start, end, alias, canonical in matches:
            if canonical in seen or any(
                start < right and end > left for left, right in occupied
            ):
                continue
            accepted.append(ResolvedEntity(canonical, alias))
            seen.add(canonical)
            occupied.append((start, end))
            if len(accepted) >= max_entities:
                break
        return tuple(accepted)

    def resolve(self, text: str, *, max_entities: int = 32) -> tuple[str, ...]:
        return tuple(
            item.canonical_id
            for item in self.resolve_text(text, max_entities=max_entities)
        )


DEFAULT_ENTITY_REGISTRY = EntityAliasRegistry(
    {
        "BTC": ("bitcoin", "btc", "xbt"),
        "ETH": ("ethereum", "ether", "eth"),
        "BNB": ("binance coin", "bnb"),
        "SOL": ("solana", "sol"),
        "XRP": ("ripple", "xrp"),
        "DOGE": ("dogecoin", "doge"),
        "NVDA": ("nvidia", "nvda"),
        "AAPL": ("apple inc", "aapl"),
        "MSFT": ("microsoft", "msft"),
        "GOOGL": ("alphabet", "google", "googl"),
        "FED": ("federal reserve", "fomc"),
        "SEC": ("u.s. securities and exchange commission", "sec"),
        "ECB": ("european central bank", "ecb"),
    }
)


__all__ = ["ResolvedEntity", "EntityAliasRegistry", "DEFAULT_ENTITY_REGISTRY"]
