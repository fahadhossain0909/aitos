"""SQLite-backed Spot inventory ledger with fill-level fee accounting.

This ledger records AITOS-managed inventory only; external wallet balances must
never be silently adopted as strategy-owned inventory.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from threading import RLock


@dataclass(frozen=True, slots=True)
class InventoryPosition:
    symbol: str
    base_asset: str
    quote_asset: str
    quantity: Decimal
    cost_quote: Decimal
    realized_quote_pnl: Decimal
    fee_conversion_pending: bool = False

    @property
    def average_cost(self) -> Decimal:
        return self.cost_quote / self.quantity if self.quantity > 0 else Decimal("0")


class InventoryLedger:
    def __init__(self, database: str | Path = ":memory:") -> None:
        self._lock = RLock()
        self._db = sqlite3.connect(str(database), check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS spot_fills (
              fill_key TEXT PRIMARY KEY, symbol TEXT NOT NULL,
              base_asset TEXT NOT NULL, quote_asset TEXT NOT NULL,
              side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
              commission TEXT NOT NULL, commission_asset TEXT NOT NULL,
              quote_value TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record_fill(self, *, fill_key: str, symbol: str, base_asset: str,
                    quote_asset: str, side: str, quantity: Decimal,
                    price: Decimal, commission: Decimal = Decimal("0"),
                    commission_asset: str = "") -> bool:
        if not fill_key or quantity <= 0 or price <= 0 or commission < 0:
            raise ValueError("invalid fill")
        side = side.upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        base_asset, quote_asset = base_asset.upper(), quote_asset.upper()
        commission_asset = commission_asset.upper()
        quote_value = quantity * price
        with self._lock:
            cur = self._db.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.execute("""INSERT OR IGNORE INTO spot_fills
                    (fill_key,symbol,base_asset,quote_asset,side,quantity,price,
                     commission,commission_asset,quote_value)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (fill_key, symbol.upper(), base_asset, quote_asset, side,
                     str(quantity), str(price), str(commission), commission_asset,
                     str(quote_value)))
                inserted = cur.rowcount == 1
                if inserted:
                    position = self._position_from_cursor(cur, symbol.upper())
                    if position.quantity < 0:
                        raise ValueError("sell exceeds AITOS-managed inventory")
                self._db.commit()
                return inserted
            except Exception:
                self._db.rollback()
                raise

    @staticmethod
    def _position_from_cursor(cur: sqlite3.Cursor, symbol: str) -> InventoryPosition:
        rows = cur.execute("""SELECT base_asset,quote_asset,side,quantity,price,
            commission,commission_asset FROM spot_fills WHERE symbol=? ORDER BY rowid""",
            (symbol,)).fetchall()
        base = quote = ""
        qty = cost = realized = Decimal("0")
        fee_conversion_pending = False
        for b, q, side, qraw, praw, craw, casset in rows:
            base, quote = b, q
            amount, price, fee = Decimal(qraw), Decimal(praw), Decimal(craw)
            value = amount * price
            if side == "BUY":
                qty += amount
                cost += value
                if casset == base:
                    qty -= fee
                elif casset == quote:
                    cost += fee
                elif fee > 0:
                    fee_conversion_pending = True
            else:
                avg = cost / qty if qty else Decimal("0")
                proceeds = value
                debit_quantity = amount
                if casset == quote:
                    proceeds -= fee
                elif casset == base:
                    debit_quantity += fee
                elif fee > 0:
                    fee_conversion_pending = True
                if debit_quantity > qty:
                    raise ValueError("sell exceeds AITOS-managed inventory")
                cost -= avg * debit_quantity
                qty -= debit_quantity
                realized += proceeds - avg * debit_quantity
        return InventoryPosition(symbol, base, quote, qty, cost, realized,
                                 fee_conversion_pending)

    def get_position(self, symbol: str) -> InventoryPosition | None:
        with self._lock:
            position = self._position_from_cursor(self._db.cursor(), symbol.upper())
            return position if position.base_asset else None
