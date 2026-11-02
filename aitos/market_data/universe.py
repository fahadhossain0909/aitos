"""Conservative, liquidity-aware Binance USDⓈ-M Futures universe resolution."""

from __future__ import annotations

from aitos.exchange.base import ExchangeAdapter

# High-liquidity, widely established contracts form the stable baseline.
# Other contracts must earn inclusion through exchange metadata and 24h activity.
CORE_USDT_PERPETUALS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "AVAXUSDT",
    "DOTUSDT",
    "TRXUSDT",
    "SUIUSDT",
    "TONUSDT",
)


def _contract_is_eligible(symbol: str, metadata: object) -> bool:
    if not symbol.endswith("USDT"):
        return False
    status = str(getattr(metadata, "status", "UNKNOWN")).upper()
    contract_type = str(getattr(metadata, "contract_type", "UNKNOWN")).upper()
    quote_asset = str(getattr(metadata, "quote_asset", "")).upper()
    # Older adapter doubles may expose only SymbolFilters. For these, only
    # the explicit core allowlist is trusted; real Binance metadata is strict.
    if status == "UNKNOWN" and contract_type == "UNKNOWN" and not quote_asset:
        return symbol in CORE_USDT_PERPETUALS
    return (
        status == "TRADING" and contract_type == "PERPETUAL" and quote_asset == "USDT"
    )


async def resolve_live_universe(
    exchange: ExchangeAdapter,
    *,
    core_symbols: tuple[str, ...] = CORE_USDT_PERPETUALS,
    min_quote_volume_usd: float = 10_000_000.0,
    min_trade_count: int = 10_000,
    dynamic_limit: int = 35,
    universe_limit: int = 100,
) -> list[str]:
    """Return core contracts plus a small, liquid, metadata-eligible dynamic set.

    When ticker statistics are available, non-core listings must pass both
    quote-volume and trade-count floors. A ticker endpoint failure fails closed
    for non-core symbols instead of falling back to alphabetical selection.
    """
    filters = await exchange.fetch_exchange_info()
    eligible = {
        str(symbol).upper(): metadata
        for symbol, metadata in filters.items()
        if _contract_is_eligible(str(symbol).upper(), metadata)
    }
    core_candidates = [s for s in core_symbols if s in eligible]
    core = list(core_candidates)
    ticker_method = getattr(exchange, "fetch_24h_tickers", None)
    dynamic: list[tuple[str, float]] = []
    if callable(ticker_method):
        try:
            tickers = await ticker_method()
            ticker_by_symbol = {str(t.get("symbol", "")).upper(): t for t in tickers}
            core = []
            for symbol in core_candidates:
                ticker = ticker_by_symbol.get(symbol, {})
                quote_volume = float(ticker.get("quoteVolume", 0.0) or 0.0)
                trade_count = int(ticker.get("count", 0) or 0)
                if (
                    quote_volume >= min_quote_volume_usd
                    and trade_count >= min_trade_count
                ):
                    core.append(symbol)
            for symbol, ticker in ticker_by_symbol.items():
                if symbol not in eligible or symbol in core_candidates:
                    continue
                quote_volume = float(ticker.get("quoteVolume", 0.0) or 0.0)
                trade_count = int(ticker.get("count", 0) or 0)
                if quote_volume >= min_quote_volume_usd and trade_count >= min_trade_count:
                    dynamic.append((symbol, quote_volume))
        except (NotImplementedError, AttributeError):
            # Adapter without ticker support: retain core only.
            dynamic = []
        except Exception:
            # Do not widen the universe on a failed liquidity lookup.
            core, dynamic = core_candidates, []
    dynamic.sort(key=lambda item: (-item[1], item[0]))
    selected = core + [s for s, _ in dynamic[: max(0, dynamic_limit)]]
    selected = list(dict.fromkeys(selected))[: max(1, universe_limit)]
    if not selected:
        raise RuntimeError(
            "exchange returned no eligible, trusted USDT perpetual symbols"
        )
    return selected
