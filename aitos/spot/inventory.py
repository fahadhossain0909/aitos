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
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS spot_fills (
              fill_key TEXT PRIMARY KEY, symbol TEXT NOT NULL,
              base_asset TEXT NOT NULL, quote_asset TEXT NOT NULL,
              side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
              commission TEXT NOT NULL, commission_asset TEXT NOT NULL,
              quote_value TEXT NOT NULL, commission_quote_value TEXT,
              fill_time_ms INTEGER, commission_conversion_symbol TEXT,
              commission_conversion_price TEXT, commission_conversion_trade_id INTEGER,
              commission_conversion_time_ms INTEGER, commission_conversion_source TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS spot_order_intents (
              client_order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
              side TEXT NOT NULL, order_type TEXT NOT NULL,
              quantity TEXT, quote_order_quantity TEXT,
              status TEXT NOT NULL, exchange_order_id INTEGER,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        columns = {
            row[1]
            for row in self._db.execute("PRAGMA table_info(spot_fills)").fetchall()
        }
        if "commission_quote_value" not in columns:
            self._db.execute(
                "ALTER TABLE spot_fills ADD COLUMN commission_quote_value TEXT"
            )
        migrations = {
            "fill_time_ms": "INTEGER",
            "commission_conversion_symbol": "TEXT",
            "commission_conversion_price": "TEXT",
            "commission_conversion_trade_id": "INTEGER",
            "commission_conversion_time_ms": "INTEGER",
            "commission_conversion_source": "TEXT",
        }
        for name, sql_type in migrations.items():
            if name not in columns:
                self._db.execute(
                    f"ALTER TABLE spot_fills ADD COLUMN {name} {sql_type}"
                )
        self._db.commit()

    def resolve_commission_conversion(
        self,
        fill_key: str,
        quote_value: Decimal,
        *,
        conversion_symbol: str | None = None,
        conversion_price: Decimal | None = None,
        conversion_trade_id: int | None = None,
        conversion_time_ms: int | None = None,
        conversion_source: str | None = None,
    ) -> None:
        """Resolve a third-asset fee using its point-in-time quote valuation.

        The caller must supply the conversion value captured at the fill time;
        current prices must never be used to rewrite historical fee accounting.
        """
        if quote_value <= 0:
            raise ValueError("commission quote value must be positive")
        with self._lock:
            cursor = self._db.execute(
                """UPDATE spot_fills SET commission_quote_value=?,
                   commission_conversion_symbol=?,
                   commission_conversion_price=?,
                   commission_conversion_trade_id=?,
                   commission_conversion_time_ms=?,
                   commission_conversion_source=?
                   WHERE fill_key=? AND commission_quote_value IS NULL
                     AND commission_asset NOT IN (
                       SELECT base_asset FROM spot_fills WHERE fill_key=?
                   ) AND commission_asset NOT IN (
                       SELECT quote_asset FROM spot_fills WHERE fill_key=?
                   )""",
                (
                    str(quote_value),
                    conversion_symbol,
                    str(conversion_price) if conversion_price is not None else None,
                    conversion_trade_id,
                    conversion_time_ms,
                    conversion_source,
                    fill_key,
                    fill_key,
                    fill_key,
                ),
            )
            self._db.commit()
            if cursor.rowcount != 1:
                raise KeyError(
                    f"unknown fill or fee is not third-asset commission: {fill_key}"
                )

    def commission_conversion_provenance(
        self, fill_key: str
    ) -> dict[str, str | int | None] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT commission_quote_value,commission_conversion_symbol,
                          commission_conversion_price,commission_conversion_trade_id,
                          commission_conversion_time_ms,commission_conversion_source
                   FROM spot_fills WHERE fill_key=?""",
                (fill_key,),
            ).fetchone()
        if row is None:
            return None
        keys = ("quote_value", "symbol", "price", "trade_id", "time_ms", "source")
        return dict(zip(keys, row))

    def managed_symbols(self) -> list[dict[str, str]]:
        """Return symbols represented by durable fill history."""
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT symbol FROM spot_fills ORDER BY symbol"
            ).fetchall()
        return [{"symbol": row[0]} for row in rows]

    def pending_fee_conversions(self) -> list[dict[str, str | int | None]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT fill_key,symbol,commission,commission_asset,base_asset,
                      quote_asset,fill_time_ms
                   FROM spot_fills
                   WHERE CAST(commission AS REAL) > 0
                     AND commission_asset NOT IN (base_asset, quote_asset)
                     AND commission_quote_value IS NULL
                   ORDER BY rowid"""
            ).fetchall()
        return [
            dict(
                zip(
                    (
                        "fill_key", "symbol", "commission", "commission_asset",
                        "base_asset", "quote_asset", "fill_time_ms"
                    ),
                    row,
                )
            )
            for row in rows
        ]

    def record_order_intent(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal | None = None,
        quote_order_quantity: Decimal | None = None,
    ) -> None:
        """Persist the client order ID before any exchange POST is attempted."""
        if not client_order_id or len(client_order_id) > 36:
            raise ValueError("valid client order ID is required")
        if (quantity is None) == (quote_order_quantity is None):
            raise ValueError("exactly one order quantity mode is required")
        values = (
            client_order_id,
            symbol.upper(),
            side.upper(),
            order_type.upper(),
            str(quantity) if quantity is not None else None,
            str(quote_order_quantity) if quote_order_quantity is not None else None,
        )
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM spot_order_intents WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if row is not None:
                raise ValueError(
                    "client order ID already exists; reconcile it instead of resubmitting"
                )
            self._db.execute(
                """INSERT INTO spot_order_intents
                (client_order_id,symbol,side,order_type,quantity,quote_order_quantity,status)
                VALUES (?,?,?,?,?,?,?)""",
                (*values, "SUBMITTING"),
            )
            self._db.commit()

    def update_order_intent(
        self, client_order_id: str, *, status: str, exchange_order_id: int | None = None
    ) -> None:
        with self._lock:
            cursor = self._db.execute(
                """UPDATE spot_order_intents
                   SET status=?, exchange_order_id=COALESCE(?,exchange_order_id),
                       updated_at=CURRENT_TIMESTAMP
                   WHERE client_order_id=?""",
                (status.upper(), exchange_order_id, client_order_id),
            )
            self._db.commit()
            if cursor.rowcount != 1:
                raise KeyError(f"unknown Spot order intent: {client_order_id}")

    def unresolved_order_intents(self) -> list[dict[str, str | int | None]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT client_order_id,symbol,side,order_type,quantity,
                          quote_order_quantity,status,exchange_order_id
                   FROM spot_order_intents
                   WHERE status NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED')
                   ORDER BY created_at"""
            ).fetchall()
        keys = (
            "client_order_id",
            "symbol",
            "side",
            "order_type",
            "quantity",
            "quote_order_quantity",
            "status",
            "exchange_order_id",
        )
        return [dict(zip(keys, row)) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record_fill(
        self,
        *,
        fill_key: str,
        symbol: str,
        base_asset: str,
        quote_asset: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal = Decimal("0"),
        commission_asset: str = "",
        fill_time_ms: int | None = None,
    ) -> bool:
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
                cur.execute(
                    """INSERT OR IGNORE INTO spot_fills
                    (fill_key,symbol,base_asset,quote_asset,side,quantity,price,
                     commission,commission_asset,quote_value,fill_time_ms)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        fill_key,
                        symbol.upper(),
                        base_asset,
                        quote_asset,
                        side,
                        str(quantity),
                        str(price),
                        str(commission),
                        commission_asset,
                        str(quote_value),
                        fill_time_ms,
                    ),
                )
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
        rows = cur.execute(
            """SELECT base_asset,quote_asset,side,quantity,price,
            commission,commission_asset,commission_quote_value
            FROM spot_fills WHERE symbol=? ORDER BY rowid""",
            (symbol,),
        ).fetchall()
        base = quote = ""
        qty = cost = realized = Decimal("0")
        fee_conversion_pending = False
        for b, q, side, qraw, praw, craw, casset, converted_fee in rows:
            base, quote = b, q
            amount, price, fee = Decimal(qraw), Decimal(praw), Decimal(craw)
            value = amount * price
            third_asset_fee = (
                Decimal(converted_fee) if converted_fee is not None else None
            )
            if side == "BUY":
                qty += amount
                cost += value
                if casset == base:
                    qty -= fee
                elif casset == quote:
                    cost += fee
                elif fee > 0:
                    if third_asset_fee is None:
                        fee_conversion_pending = True
                    else:
                        cost += third_asset_fee
            else:
                avg = cost / qty if qty else Decimal("0")
                proceeds = value
                debit_quantity = amount
                if casset == quote:
                    proceeds -= fee
                elif casset == base:
                    debit_quantity += fee
                elif fee > 0:
                    if third_asset_fee is None:
                        fee_conversion_pending = True
                    else:
                        proceeds -= third_asset_fee
                if debit_quantity > qty:
                    raise ValueError("sell exceeds AITOS-managed inventory")
                cost -= avg * debit_quantity
                qty -= debit_quantity
                realized += proceeds - avg * debit_quantity
        return InventoryPosition(
            symbol, base, quote, qty, cost, realized, fee_conversion_pending
        )

    def get_position(self, symbol: str) -> InventoryPosition | None:
        with self._lock:
            position = self._position_from_cursor(self._db.cursor(), symbol.upper())
            return position if position.base_asset else None
