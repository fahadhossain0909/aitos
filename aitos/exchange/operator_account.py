"""Read-only Binance account monitor adapter; opt-in, not a trading executor.

Inject a GET-only signed transport outside the webview. No credentials, order
methods or automatic runtime wiring here. A response is one bounded observation,
not a consistent cross-endpoint snapshot, historical timeline or recovery proof.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import re

from aitos.trading.account_reconciliation import amount


class AccountObservationUnavailable(RuntimeError):
    pass


def _money(row, key, *, nonnegative=False):
    value = row[key]
    parsed = amount(value)
    if nonnegative and parsed < 0:
        raise ValueError("nonnegative value required")
    return value


class BinanceOperatorAccount:
    def __init__(self, signed_get):
        # Deliberately accept no executor/client object with order authority.
        if not callable(signed_get):
            raise TypeError("explicit GET-only transport required")
        self._get = signed_get
        self._lock = asyncio.Lock()

    async def observe(self):
        if self._lock.locked():
            raise AccountObservationUnavailable("observation already in progress")
        async with self._lock:
            try:
                tasks = [
                    asyncio.create_task(self._get(path, {}))
                    for path in ("/fapi/v3/account", "/fapi/v3/positionRisk")
                ]
                try:
                    account, positions = await asyncio.wait_for(
                        asyncio.gather(*tasks), timeout=10
                    )
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                if (
                    type(account) is not dict
                    or type(positions) is not list
                    or len(positions) > 2000
                ):
                    raise ValueError("bounded provider response required")
                totals = {
                    key: _money(
                        account, key, nonnegative=key != "totalUnrealizedProfit"
                    )
                    for key in (
                        "totalWalletBalance",
                        "totalUnrealizedProfit",
                        "totalMarginBalance",
                        "totalInitialMargin",
                        "totalMaintMargin",
                        "availableBalance",
                    )
                }
                # This identity is provider-local; do not combine different cuts
                # or replace a discrepancy with computed/zero account balances.
                with localcontext() as context:
                    context.prec = 64
                    if Decimal(totals["totalWalletBalance"]) + Decimal(
                        totals["totalUnrealizedProfit"]
                    ) != Decimal(totals["totalMarginBalance"]):
                        raise ValueError("inconsistent account totals")
                rows, seen = [], set()
                for row in positions:
                    symbol, side = row["symbol"], row["positionSide"]
                    if (
                        not isinstance(symbol, str)
                        or not re.fullmatch(r"[A-Z0-9]{2,24}", symbol)
                        or side not in ("BOTH", "LONG", "SHORT")
                    ):
                        raise ValueError("invalid position identity")
                    if (symbol, side) in seen:
                        raise ValueError("duplicate position")
                    seen.add((symbol, side))
                    quantity = _money(row, "positionAmt")
                    values = {
                        key: _money(
                            row, key, nonnegative=key in ("markPrice", "entryPrice")
                        )
                        for key in ("markPrice", "entryPrice", "unRealizedProfit")
                    }
                    if Decimal(quantity) == 0:
                        continue
                    if Decimal(values["markPrice"]) <= 0:
                        raise ValueError("missing open-position mark")
                    rows.append(
                        dict(symbol=symbol, side=side, quantity=quantity, **values)
                    )
                return {
                    "schema_version": 1,
                    "source": "binance_signed_rest",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "controls": "read_only",
                    "consistent_snapshot": False,
                    "totals": totals,
                    "positions": rows,
                    "limitations": [
                        "cross_endpoint_cut_not_atomic",
                        "not_historical",
                        "protection_and_realized_pnl_not_collected",
                    ],
                }
            except Exception:
                # No provider text/account identifiers/keys escape to callers.
                raise AccountObservationUnavailable(
                    "account observation unavailable"
                ) from None
