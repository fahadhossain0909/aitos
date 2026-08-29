"""Deterministic crypto sector classification used by the risk engine.

The risk layer must never silently put a live position into an implicit
``unclassified`` bucket.  Symbols known to the system are mapped to a stable
sector taxonomy; unknown symbols fall back to ``other`` so they still share a
risk bucket and remain subject to the sector cap.
"""

from __future__ import annotations

from collections.abc import Mapping

# Conservative, strategy-oriented crypto sectors.  This is deliberately local
# and deterministic so a missing exchange metadata call can never disable a
# risk control.
SYMBOL_SECTORS: Mapping[str, str] = {
    # Major assets
    "BTCUSDT": "crypto-major",
    "ETHUSDT": "crypto-major",
    # Layer-1 / smart-contract platforms
    "SOLUSDT": "layer1",
    "BNBUSDT": "exchange-token",
    "ADAUSDT": "layer1",
    "AVAXUSDT": "layer1",
    "DOTUSDT": "layer1",
    "ATOMUSDT": "layer1",
    "NEARUSDT": "layer1",
    "APTUSDT": "layer1",
    "SUIUSDT": "layer1",
    "SEIUSDT": "layer1",
    "TONUSDT": "layer1",
    "TRXUSDT": "layer1",
    "XRPUSDT": "payments",
    # Oracles / infrastructure
    "LINKUSDT": "oracle-infrastructure",
    "FILUSDT": "storage-infrastructure",
    "ARUSDT": "storage-infrastructure",
    # DeFi
    "UNIUSDT": "defi",
    "AAVEUSDT": "defi",
    "MKRUSDT": "defi",
    "CRVUSDT": "defi",
    "LDOUSDT": "defi",
    "SUSHIUSDT": "defi",
    # Meme
    "DOGEUSDT": "meme",
    "SHIBUSDT": "meme",
    "PEPEUSDT": "meme",
    "BONKUSDT": "meme",
    "WIFUSDT": "meme",
    # Stablecoins (normally not traded directionally, but classified safely)
    "USDCUSDT": "stablecoin",
    "USDTUSDT": "stablecoin",
    "DAIUSDT": "stablecoin",
}


def normalize_symbol(symbol: str) -> str:
    """Normalize exchange symbols for deterministic lookup."""
    return symbol.replace("/", "").replace("-", "").replace("_", "").upper()


def sector_for_symbol(symbol: str) -> str:
    """Return the risk sector for a trading symbol.

    Unknown assets are deliberately assigned to ``other`` rather than an
    implicit/unbounded ``unclassified`` bucket.  This keeps the sector cap
    effective even when a new symbol is introduced before its taxonomy entry
    is added.
    """
    normalized = normalize_symbol(symbol)
    return SYMBOL_SECTORS.get(normalized, "other")
