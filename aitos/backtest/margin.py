"""Deterministic perpetual-futures margin, funding and liquidation model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarginSnapshot:
    wallet_balance: float
    position_qty: float
    entry_price: float
    mark_price: float
    leverage: float
    initial_margin: float
    maintenance_margin: float
    unrealized_pnl: float
    funding_paid: float
    equity: float
    liquidation_price: float | None
    liquidated: bool


class PerpetualMarginModel:
    def __init__(
        self,
        wallet_balance: float,
        leverage: float = 1.0,
        maintenance_rate: float = 0.005,
    ) -> None:
        if wallet_balance <= 0 or leverage <= 0 or maintenance_rate < 0:
            raise ValueError("invalid margin configuration")
        self.wallet_balance = wallet_balance
        self.leverage = leverage
        self.maintenance_rate = maintenance_rate
        self.position_qty = 0.0
        self.entry_price = 0.0
        self.funding_paid = 0.0
        self.liquidated = False

    def open_or_add(self, signed_qty: float, price: float) -> None:
        if self.liquidated or signed_qty == 0 or price <= 0:
            raise ValueError("invalid position update")
        old = self.position_qty
        new = old + signed_qty
        if old == 0 or (old > 0 and signed_qty > 0) or (old < 0 and signed_qty < 0):
            self.entry_price = (
                (abs(old) * self.entry_price) + (abs(signed_qty) * price)
            ) / abs(new)
        else:
            close = min(abs(old), abs(signed_qty))
            direction = 1 if old > 0 else -1
            self.wallet_balance += close * (price - self.entry_price) * direction
            if new == 0:
                self.entry_price = 0.0
            elif (old > 0) != (new > 0):
                self.entry_price = price
        self.position_qty = new

    def apply_funding(self, funding_rate: float, mark_price: float) -> float:
        if mark_price <= 0:
            raise ValueError("mark_price must be positive")
        payment = self.position_qty * mark_price * funding_rate
        self.wallet_balance -= payment
        self.funding_paid += payment
        return payment

    def snapshot(self, mark_price: float) -> MarginSnapshot:
        if mark_price <= 0:
            raise ValueError("mark_price must be positive")
        notional = abs(self.position_qty) * mark_price
        initial_margin = (
            abs(self.position_qty) * self.entry_price / self.leverage
            if self.position_qty
            else 0.0
        )
        maintenance = notional * self.maintenance_rate
        unrealized = self.position_qty * (mark_price - self.entry_price)
        equity = self.wallet_balance + unrealized
        liq = None
        if self.position_qty:
            direction = 1 if self.position_qty > 0 else -1
            buffer = max(0.0, self.wallet_balance - maintenance) / abs(
                self.position_qty
            )
            liq = self.entry_price - direction * buffer
        liquidated = bool(self.position_qty and equity <= maintenance)
        return MarginSnapshot(
            self.wallet_balance,
            self.position_qty,
            self.entry_price,
            mark_price,
            self.leverage,
            initial_margin,
            maintenance,
            unrealized,
            self.funding_paid,
            equity,
            liq,
            liquidated,
        )

    def check_liquidation(self, mark_price: float) -> bool:
        snap = self.snapshot(mark_price)
        if snap.liquidated:
            self.liquidated = True
            self.position_qty = 0.0
        return self.liquidated
