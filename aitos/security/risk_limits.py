"""Independent signer-side risk preflight and account-evidence contracts.

This module is intentionally pure: evidence must come from a separately
authenticated exchange/account adapter. Missing, stale or inconsistent evidence
is a hard rejection. It does not submit orders or claim venue reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Protocol

from aitos.security.signing_policy import IntentRejected, ValidatedOrderIntent


@dataclass(frozen=True)
class AccountRiskSnapshot:
    account_alias: str
    captured_at_ms: int
    gross_exposure: Decimal
    symbol_exposure: Mapping[str, Decimal]
    daily_loss: Decimal
    order_count_window: int
    positions: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        # Copy and freeze mappings so evidence cannot be changed after capture.
        if isinstance(self.symbol_exposure, Mapping):
            object.__setattr__(
                self, "symbol_exposure", MappingProxyType(dict(self.symbol_exposure))
            )
        if isinstance(self.positions, Mapping):
            object.__setattr__(
                self, "positions", MappingProxyType(dict(self.positions))
            )


@dataclass(frozen=True)
class SignerRiskLimits:
    max_gross_exposure: Decimal
    max_symbol_exposure: Decimal
    max_daily_loss: Decimal
    max_orders_per_window: int
    max_snapshot_age_ms: int = 5_000

    def __post_init__(self) -> None:
        for name in (
            "max_gross_exposure",
            "max_symbol_exposure",
            "max_daily_loss",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError("positive_decimal_risk_limit_required")
        if (
            isinstance(self.max_orders_per_window, bool)
            or not isinstance(self.max_orders_per_window, int)
            or self.max_orders_per_window < 1
        ):
            raise ValueError("invalid_order_rate_limit")
        if (
            isinstance(self.max_snapshot_age_ms, bool)
            or not isinstance(self.max_snapshot_age_ms, int)
            or not 1 <= self.max_snapshot_age_ms <= 60_000
        ):
            raise ValueError("invalid_snapshot_age_limit")

    def authorize(
        self,
        intent: ValidatedOrderIntent,
        snapshot: AccountRiskSnapshot,
        *,
        now_ms: int,
    ) -> None:
        """Raise IntentRejected unless independent account/risk evidence permits."""
        if not isinstance(snapshot, AccountRiskSnapshot):
            raise IntentRejected("account_evidence_required")
        if not isinstance(snapshot.symbol_exposure, Mapping) or not isinstance(
            snapshot.positions, Mapping
        ):
            raise IntentRejected("account_evidence_invalid")
        if any(
            not isinstance(symbol, str) or not symbol
            for symbol in (*snapshot.symbol_exposure.keys(), *snapshot.positions.keys())
        ):
            raise IntentRejected("account_evidence_invalid")
        if snapshot.account_alias != intent.account_alias:
            raise IntentRejected("account_evidence_mismatch")
        if (
            isinstance(now_ms, bool)
            or not isinstance(now_ms, int)
            or isinstance(snapshot.captured_at_ms, bool)
            or not isinstance(snapshot.captured_at_ms, int)
            or snapshot.captured_at_ms > now_ms
            or now_ms - snapshot.captured_at_ms > self.max_snapshot_age_ms
        ):
            raise IntentRejected("account_evidence_stale")
        for value in (
            snapshot.gross_exposure,
            snapshot.daily_loss,
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise IntentRejected("account_evidence_invalid")
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in snapshot.symbol_exposure.values()
        ):
            raise IntentRejected("account_evidence_invalid")
        if (
            isinstance(snapshot.order_count_window, bool)
            or not isinstance(snapshot.order_count_window, int)
            or snapshot.order_count_window < 0
        ):
            raise IntentRejected("account_evidence_invalid")
        if snapshot.gross_exposure > self.max_gross_exposure:
            raise IntentRejected("gross_exposure_limit_exceeded")
        if snapshot.daily_loss >= self.max_daily_loss:
            raise IntentRejected("daily_loss_limit_reached")
        if snapshot.order_count_window >= self.max_orders_per_window:
            raise IntentRejected("order_rate_limit_reached")

        if any(
            not isinstance(position, Decimal) or not position.is_finite()
            for position in snapshot.positions.values()
        ):
            raise IntentRejected("account_evidence_invalid")
        current_symbol = snapshot.symbol_exposure.get(intent.symbol, Decimal("0"))
        if (
            not isinstance(current_symbol, Decimal)
            or not current_symbol.is_finite()
            or current_symbol < 0
        ):
            raise IntentRejected("account_evidence_invalid")
        if intent.reduce_only:
            position = snapshot.positions.get(intent.symbol)
            if (
                not isinstance(position, Decimal)
                or not position.is_finite()
                or position == 0
            ):
                raise IntentRejected("reduce_only_position_evidence_required")
            if (position > 0 and intent.side != "SELL") or (
                position < 0 and intent.side != "BUY"
            ):
                raise IntentRejected("reduce_only_side_would_increase_exposure")
            if intent.quantity > abs(position):
                raise IntentRejected("reduce_only_quantity_exceeds_position")
            return

        if snapshot.gross_exposure + intent.notional > self.max_gross_exposure:
            raise IntentRejected("gross_exposure_limit_exceeded")
        if current_symbol + intent.notional > self.max_symbol_exposure:
            raise IntentRejected("symbol_exposure_limit_exceeded")


class AccountEvidenceProvider(Protocol):
    """Trusted local adapter; implementations must query the exchange directly."""

    def get_snapshot(self, account_alias: str, *, now_ms: int) -> AccountRiskSnapshot:
        """Return reconciled account evidence or raise; never use VPS claims."""
        ...
