"""Testable logic behind ``run_live_trading.py``. Split out from the
script itself so the confirmation gate and executor-preparation logic can
be unit tested without needing a real terminal or real Binance
credentials — see ``tests/test_live_trading.py``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from aitos.exchange.binance import BinanceFuturesAdapter
from aitos.execution.binance_executor import BinanceFuturesOrderExecutor
from aitos.logging_setup import get_logger

logger = get_logger("aitos.live_trading")

CONFIRMATION_PHRASE = "I APPROVE LIVE TRADING"


def confirm_live_trading(
    symbols: list[str], testnet: bool, input_fn: Callable[[str], str] = input
) -> str:
    """Interactive, session-level human approval gate. Returns the
    operator's identifier to use as every opportunity's ``approved_by``
    for the run. Exits the process (via ``sys.exit(1)``) on any failed
    confirmation rather than returning a falsy value — a caller
    forgetting to check a return value must never be how this gate fails
    open.
    """
    target = "TESTNET" if testnet else "MAINNET — REAL MONEY"
    print("=" * 70)
    print(f"AITOS is about to start LIVE trading against Binance {target}.")
    print(f"Symbols: {', '.join(symbols)}")
    print("Real orders will be placed on your account.")
    print("=" * 70)

    operator = input_fn(
        "Type your name/identifier to approve this session (or Ctrl-C to abort): "
    ).strip()
    if not operator:
        print("No identifier entered — aborting.")
        sys.exit(1)

    confirmation = input_fn(
        f"Type EXACTLY '{CONFIRMATION_PHRASE}' to proceed as '{operator}': "
    ).strip()
    if confirmation != CONFIRMATION_PHRASE:
        print("Confirmation text did not match — aborting.")
        sys.exit(1)

    return operator


async def prepare_live_executor(
    settings, symbols: list[str]
) -> BinanceFuturesOrderExecutor:
    """Construct, connect, and fully prepare a live executor: verifies
    credentials exist, verifies the account's actual hedge-mode setting
    matches configuration (refusing to trade on a mismatched assumption
    rather than guessing), and loads real per-symbol precision from
    ``/fapi/v1/exchangeInfo`` before returning.
    """
    if not settings.binance.api_key or not settings.binance.api_secret:
        logger.error(
            "BINANCE_API_KEY/BINANCE_API_SECRET are not set — cannot trade live"
        )
        sys.exit(1)

    executor = BinanceFuturesOrderExecutor(
        api_key=settings.binance.api_key,
        api_secret=settings.binance.api_secret,
        testnet=settings.binance.testnet,
        recv_window_ms=settings.binance.recv_window_ms,
        hedge_mode=settings.binance.hedge_mode,
    )
    await executor.connect()

    account_hedge_mode = await executor.get_position_mode()
    if account_hedge_mode != settings.binance.hedge_mode:
        logger.error(
            "BINANCE_HEDGE_MODE (%s) does not match the account's actual setting (%s) — refusing to trade with mismatched assumptions",
            settings.binance.hedge_mode,
            account_hedge_mode,
        )
        await executor.close()
        sys.exit(1)

    info_adapter = BinanceFuturesAdapter()
    async with info_adapter:
        symbol_filters = await info_adapter.fetch_exchange_info(symbols=symbols)
    executor.load_symbol_filters(symbol_filters)
    logger.info(
        "loaded exchangeInfo precision",
        extra={"aitos_extra": {"symbols": list(symbol_filters.keys())}},
    )

    return executor
