"""Conservative, liquidity-aware Binance USDⓈ-M Futures universe resolution."""

from __future__ import annotations

import time

from aitos.config.tuning import DEFAULT_CORE_FUTURES_SYMBOLS
from aitos.exchange.base import ExchangeAdapter

# High-liquidity, widely established contracts form the stable baseline.
# Other contracts must earn inclusion through exchange metadata and 24h activity.
CORE_USDT_PERPETUALS = DEFAULT_CORE_FUTURES_SYMBOLS


def _contract_is_eligible(
    symbol: str,
    metadata: object,
    *,
    core_symbols: tuple[str, ...],
    min_contract_age_days: int,
) -> bool:
    if not symbol.endswith("USDT"):
        return False
    status = str(getattr(metadata, "status", "UNKNOWN")).upper()
    contract_type = str(getattr(metadata, "contract_type", "UNKNOWN")).upper()
    quote_asset = str(getattr(metadata, "quote_asset", "")).upper()
    # Older adapter doubles may expose only SymbolFilters. For these, only
    # the explicit core allowlist is trusted; real Binance metadata is strict.
    if status == "UNKNOWN" and contract_type == "UNKNOWN" and not quote_asset:
        return symbol in core_symbols
    if not (
        status == "TRADING" and contract_type == "PERPETUAL" and quote_asset == "USDT"
    ):
        return False
    if symbol not in core_symbols and min_contract_age_days > 0:
        onboard_date = getattr(metadata, "onboard_date", None)
        if onboard_date is None:
            return False
        minimum_onboard_date = int(time.time() * 1000) - min_contract_age_days * 86_400_000
        if int(onboard_date) > minimum_onboard_date:
            return False
    return True


async def resolve_live_universe(
    exchange: ExchangeAdapter,
    *,
    core_symbols: tuple[str, ...] = CORE_USDT_PERPETUALS,
    min_quote_volume_usd: float = 10_000_000.0,
    min_trade_count: int = 10_000,
    min_contract_age_days: int = 30,
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
        if _contract_is_eligible(
            str(symbol).upper(),
            metadata,
            core_symbols=core_symbols,
            min_contract_age_days=min_contract_age_days,
        )
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
                if (
                    quote_volume >= min_quote_volume_usd
                    and trade_count >= min_trade_count
                ):
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
