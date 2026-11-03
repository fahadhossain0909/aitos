"""Exchange-agnostic L2 sequence validation and recovery planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

GapKind = Literal["none", "duplicate", "stale", "gap"]


@dataclass(frozen=True)
class SequenceCheck:
    kind: GapKind
    previous: int | None
    current: int | None
    expected: int | None

    @property
    def requires_recovery(self) -> bool:
        return self.kind == "gap"


class L2SequenceValidator:
    """Validate contiguous numeric update IDs.

    The validator deliberately does not assume that every exchange uses the
    same sequence semantics. Adapters should configure whether a contiguous
    increment is required. A gap must stop replay until a fresh snapshot is
    obtained; silently continuing would corrupt the reconstructed book.
    """

    def __init__(self, require_contiguous: bool = True) -> None:
        self.require_contiguous = require_contiguous
        self.last: int | None = None

    def reset(self, update_id: int | str | None = None) -> None:
        self.last = self._int(update_id)

    @staticmethod
    def _int(value: int | str | None) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def check(self, update_id: int | str | None) -> SequenceCheck:
        current = self._int(update_id)
        if current is None or self.last is None:
            self.last = current
            return SequenceCheck("none", self.last, current, None)
        if current == self.last:
            return SequenceCheck("duplicate", self.last, current, self.last + 1)
        if current < self.last:
            return SequenceCheck("stale", self.last, current, self.last + 1)
        expected = self.last + 1
        if self.require_contiguous and current != expected:
            return SequenceCheck("gap", self.last, current, expected)
        result = SequenceCheck("none", self.last, current, expected)
        self.last = current
        return result


@dataclass(frozen=True)
class RecoveryRequest:
    exchange: str
    symbol: str
    market: str
    reason: str
    last_update_id: int | None
    observed_update_id: int | None


class L2RecoveryPlanner:
    """Turn sequence gaps into explicit snapshot-recovery requests."""

    def request(
        self, exchange: str, symbol: str, market: str, check: SequenceCheck
    ) -> RecoveryRequest | None:
        if not check.requires_recovery:
            return None
        return RecoveryRequest(
            exchange, symbol, market, "l2_sequence_gap", check.previous, check.current
        )
