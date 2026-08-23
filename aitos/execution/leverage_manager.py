"""Session-start leverage synchronization for Binance Futures."""

from __future__ import annotations

from typing import Iterable

from aitos.execution.binance_executor import BinanceFuturesOrderExecutor
from aitos.logging_setup import get_logger

logger = get_logger("aitos.execution.leverage_manager")


async def configure_session_leverage(
    executor: BinanceFuturesOrderExecutor,
    symbols: Iterable[str],
    configured_max_leverage: float,
) -> None:
    """Set every traded symbol to the configured risk-tier leverage.

    The risk engine's ``max_leverage`` is the single configured ceiling used
    for the session. Binance remains authoritative about the symbol-specific
    maximum; an API rejection is surfaced instead of silently trading with
    an unexpected leverage setting.
    """
    leverage = int(configured_max_leverage)
    if leverage < 1:
        raise ValueError("configured leverage must be at least 1")

    for symbol in symbols:
        response = await executor.set_leverage(symbol, leverage)
        logger.info(
            "configured Binance leverage",
            extra={
                "aitos_extra": {
                    "symbol": symbol,
                    "requested_leverage": leverage,
                    "effective_leverage": response.get("leverage"),
                    "max_notional_value": response.get("maxNotionalValue"),
                }
            },
        )
