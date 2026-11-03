"""Exchange-specific L2 sequence policies."""

from __future__ import annotations

from dataclasses import dataclass

from .l2_sequence import L2SequenceValidator, RecoveryRequest


@dataclass
class ExchangeL2Adapter:
    exchange: str
    market: str
    symbol: str
    validator: L2SequenceValidator

    def validate(self, update_id: int | str | None):
        return self.validator.check(update_id)

    def recovery_request(self, check) -> RecoveryRequest | None:
        if not check.requires_recovery:
            return None
        return RecoveryRequest(
            self.exchange,
            self.symbol,
            self.market,
            "l2_sequence_gap",
            check.previous,
            check.current,
        )


class BinanceL2Adapter(ExchangeL2Adapter):
    def __init__(self, symbol: str, market: str = "futures_um"):
        super().__init__(
            "binance",
            market,
            symbol.upper(),
            L2SequenceValidator(require_contiguous=True),
        )


class BybitL2Adapter(ExchangeL2Adapter):
    def __init__(self, symbol: str, market: str = "linear"):
        # Bybit's public order-book streams expose sequence identifiers, but
        # their exact semantics vary by stream/version; recovery is therefore
        # driven by an explicit adapter policy rather than hidden assumptions.
        super().__init__(
            "bybit",
            market,
            symbol.upper(),
            L2SequenceValidator(require_contiguous=True),
        )
