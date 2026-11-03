"""Deterministic execution and portfolio accounting primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class Fill:
    side: Side
    quantity: float
    price: float
    fee: float


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash: float
    position_qty: float
    avg_entry: float
    realized_pnl: float
    fees: float
    equity: float


class ExecutionSimulator:
    def __init__(
        self, initial_cash: float, fee_rate: float = 0.0004, slippage_bps: float = 0.0
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if fee_rate < 0 or slippage_bps < 0:
            raise ValueError("fee_rate and slippage_bps must be non-negative")
        self.cash = initial_cash
        self.position_qty = 0.0
        self.avg_entry = 0.0
        self.realized_pnl = 0.0
        self.fees = 0.0
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps

    def execute(self, side: Side, quantity: float, market_price: float) -> Fill:
        if quantity <= 0 or market_price <= 0:
            raise ValueError("quantity and market_price must be positive")
        impact = self.slippage_bps / 10000.0
        price = market_price * (1 + impact if side == "buy" else 1 - impact)
        fee = price * quantity * self.fee_rate
        signed = quantity if side == "buy" else -quantity
        old = self.position_qty
        new = old + signed
        if old == 0 or (old > 0 and signed > 0) or (old < 0 and signed < 0):
            self.avg_entry = (
                ((abs(old) * self.avg_entry) + (abs(signed) * price)) / abs(new)
                if new
                else 0.0
            )
        else:
            closing = min(abs(old), abs(signed))
            direction = 1 if old > 0 else -1
            self.realized_pnl += closing * (price - self.avg_entry) * direction
            if new != 0 and (old > 0) != (new > 0):
                self.avg_entry = price
        self.cash -= signed * price + fee
        self.fees += fee
        self.position_qty = new
        return Fill(side, quantity, price, fee)

    def snapshot(self, mark_price: float) -> PortfolioSnapshot:
        if mark_price <= 0:
            raise ValueError("mark_price must be positive")
        unrealized = self.position_qty * (mark_price - self.avg_entry)
        equity = self.cash + self.position_qty * mark_price
        return PortfolioSnapshot(
            self.cash,
            self.position_qty,
            self.avg_entry,
            self.realized_pnl + unrealized,
            self.fees,
            equity,
        )
