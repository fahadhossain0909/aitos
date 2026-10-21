"""Bridge between the A-Stat statistical stack and the live scanner.

A-Stat (aitos/statistics) supplies direction probability, GARCH volatility,
EVT tail risk and regime state. This adapter converts scanner-native
measurements into A-Stat features, runs the per-contract statistical stack
(HMM/GARCH/EVT/Bayes over kline returns) and maps the result onto the
scanner's 0-10 component scale, signed by the candidate's proposed side.

The mapping is deliberately simple and explainable:

    component = 10 * P(statistical direction agrees with proposed side)

A neutral 5.0 means the statistical layer is indifferent; < 5 penalises
entries that fight the statistical evidence.
"""

from __future__ import annotations

import math

from aitos.logging_setup import get_logger
from aitos.models.trade import TradeSide
from aitos.statistics.engine import AStatEngine
from aitos.statistics.models import AStatObservation, AStatResult

logger = get_logger("aitos.intelligence.a_stat")

MIN_KLINES_FOR_STACK = 30
MOMENTUM_WINDOW = 20
# Scanner component scores live on 0-10; the engine's weighted feature score
# expects small signed magnitudes, so 0-10 maps to roughly [-2, 2].
_COMPONENT_TO_SIGNED = 2.0 / 5.0


def _signed(component_score: float) -> float:
    return (float(component_score) - 5.0) * _COMPONENT_TO_SIGNED


class AStatBridge:
    """Per-process adapter around one shared AStatEngine."""

    def __init__(self, calibration_window: int = 256) -> None:
        self._engine = AStatEngine(calibration_window=calibration_window)
        self._last_kline_count: dict[str, int] = {}

    @property
    def engine(self) -> AStatEngine:
        return self._engine

    def evaluate(
        self,
        *,
        symbol: str,
        direction: TradeSide,
        klines,
        flow_score: float,
        candle_cvd: float,
        orderflow_vwap: float,
        depth_imbalance: float,
        funding_score: float,
        oi_score: float,
        lead_lag: float,
        last_close: float,
    ) -> tuple[float | None, AStatResult | None]:
        """Return (0-10 scanner component, AStatResult) or (None, None).

        ``None`` means the statistical stack could not produce a usable
        estimate (not enough history) — the scanner then simply excludes
        the component and renormalises the remaining weights.
        """
        closes = [float(k.close) for k in klines]
        if any(not math.isfinite(value) for value in closes) or len(closes) < 2:
            return None, None
        returns = tuple(
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1] > 0.0
            and math.isfinite(closes[i])
            and math.isfinite(closes[i - 1])
        )
        if len(returns) < MIN_KLINES_FOR_STACK:
            return None, None
        # Avoid re-feeding the same overlapping return window on every scan.
        # The statistical stack accumulates history internally, so only new
        # returns should be appended.
        previous_klines = self._last_kline_count.get(symbol, 0)
        if previous_klines <= 0 or len(closes) <= previous_klines:
            new_returns = returns if previous_klines <= 0 else ()
        else:
            new_returns = returns[previous_klines - 1 :]
        self._last_kline_count[symbol] = len(closes)
        window = returns[-MOMENTUM_WINDOW:]
        momentum = sum(window)
        if orderflow_vwap and orderflow_vwap > 0.0 and last_close > 0.0:
            vwap_distance = (last_close - orderflow_vwap) / orderflow_vwap
        else:
            vwap_distance = 0.0
        # Funding and open interest are side-fitness scores. Convert them to
        # market-directional evidence for the proposed side.
        if direction is TradeSide.LONG:
            funding_feature = -_signed(funding_score)
            oi_feature = _signed(oi_score)
        else:
            funding_feature = _signed(funding_score)
            oi_feature = -_signed(oi_score)
        features = {
            "momentum": momentum,
            "return": returns[-1],
            "orderbook_imbalance": _signed(depth_imbalance),
            "cvd": _signed(candle_cvd),
            "delta": _signed(flow_score),
            "vwap_distance": vwap_distance,
            "funding": funding_feature,
            "open_interest": oi_feature,
            "lead_lag": _signed(lead_lag),
            "returns": new_returns,
        }
        try:
            result = self._engine.evaluate(
                AStatObservation(
                    symbol=symbol,
                    features=features,
                    prior_up=0.5,
                    sample_size=len(returns),
                )
            )
        except Exception:
            logger.warning(
                "a_stat evaluation failed",
                exc_info=True,
                extra={"aitos_extra": {"symbol": symbol}},
            )
            return None, None
        agree = (
            result.direction.up
            if direction is TradeSide.LONG
            else result.direction.down
        )
        component = round(10.0 * agree, 2)
        logger.info(
            "a_stat evaluation",
            extra={
                "aitos_extra": {
                    "symbol": symbol,
                    "proposed_side": direction.value,
                    "component_score": component,
                    "p_up": round(result.direction.up, 4),
                    "p_down": round(result.direction.down, 4),
                    "p_flat": round(result.direction.flat, 4),
                    "expected_value": round(result.expected_value, 8),
                    "expected_volatility": round(result.expected_volatility, 8),
                    "downside_probability": round(result.downside_probability, 4),
                    "tail_probability": round(result.tail_loss_probability, 4),
                    "regime_trend_up": round(result.regime.trend_up, 4),
                    "regime_trend_down": round(result.regime.trend_down, 4),
                    "regime_high_vol": round(result.regime.high_volatility, 4),
                    "confidence": round(result.probability_confidence, 4),
                    "advanced_stack": result.advanced is not None,
                }
            },
        )
        return component, result

    def update(self, realised_return: float, predicted_up: float) -> None:
        """Feed a realised outcome back into the online calibration."""
        self._engine.update(realised_return, predicted_up)
