"""Dynamic Position Sizing + Adaptive Leverage — spec section 30.2."""

from __future__ import annotations

from aitos.risk.models import PositionSizeResult, RiskLimits


def kelly_fraction(win_rate: float, win_loss_ratio: float) -> float:
    """Classic Kelly fraction: f* = W - (1-W)/R, clamped to [0, 1]."""
    if not 0.0 <= win_rate <= 1.0:
        raise ValueError("win_rate must be within [0.0, 1.0]")
    if win_loss_ratio <= 0:
        raise ValueError("win_loss_ratio must be positive")
    f = win_rate - (1.0 - win_rate) / win_loss_ratio
    return max(0.0, min(f, 1.0))


def calculate_adaptive_leverage(
    volatility_percentile: float,
    risk_score: float,
    risk_limits: RiskLimits,
    base_leverage: float = 10.0,
) -> float:
    """Leverage shrinks as volatility and/or risk score rise."""
    volatility_percentile = max(0.0, min(volatility_percentile, 100.0))
    risk_score = max(0.0, min(risk_score, 100.0))
    vol_damp = 1.0 - (volatility_percentile / 100.0) * 0.8
    risk_damp = 1.0 - (risk_score / 100.0) * 0.9
    leverage = base_leverage * vol_damp * risk_damp
    return round(max(1.0, min(leverage, risk_limits.max_leverage)), 2)


def calculate_position_size(
    equity_usd: float,
    entry_price: float,
    stop_loss_price: float,
    risk_limits: RiskLimits,
    risk_score: float = 0.0,
    win_rate: float | None = None,
    win_loss_ratio: float | None = None,
    volatility_percentile: float = 50.0,
    correlation_penalty: float = 0.0,
    requested_risk_pct: float | None = None,
    base_leverage: float = 10.0,
    existing_sector_notional_usd: float = 0.0,
    sector_limit_pct: float | None = None,
) -> PositionSizeResult:
    """Compute a position size in USD *notional* plus leverage.

    The sector cap is applied to notional, not margin.  This is intentional:
    leverage is already controlled separately, while sector exposure measures
    the actual market notional concentrated in one sector.  The projected
    notional is therefore capped before an order can reach the executor.
    """
    if equity_usd <= 0:
        raise ValueError("equity_usd must be positive")
    stop_distance = abs(entry_price - stop_loss_price)
    if stop_distance <= 0:
        raise ValueError("stop_loss_price must differ from entry_price")
    if existing_sector_notional_usd < 0:
        raise ValueError("existing_sector_notional_usd cannot be negative")

    risk_pct = (
        requested_risk_pct
        if requested_risk_pct is not None
        else risk_limits.max_risk_per_trade_pct
    )
    risk_pct = min(risk_pct, risk_limits.max_risk_per_trade_hard_cap_pct)

    kelly_note = ""
    if win_rate is not None and win_loss_ratio is not None:
        kf = kelly_fraction(win_rate, win_loss_ratio)
        kelly_scalar = max(min(kf / 0.5, 1.0), 0.1) if kf > 0 else 0.1
        risk_pct *= kelly_scalar
        kelly_note = f", kelly_fraction={kf:.3f} (scalar={kelly_scalar:.2f})"

    vol_factor = 1.0 - (max(0.0, min(volatility_percentile, 100.0)) / 100.0) * 0.5
    corr_factor = 1.0 - max(0.0, min(correlation_penalty, 1.0)) * 0.5
    risk_pct *= vol_factor * corr_factor

    risk_amount_usd = equity_usd * (risk_pct / 100.0)
    units = risk_amount_usd / stop_distance
    position_size_usd = units * entry_price

    leverage = calculate_adaptive_leverage(
        volatility_percentile, risk_score, risk_limits, base_leverage
    )
    max_notional_usd = equity_usd * leverage
    capped_by_leverage = position_size_usd > max_notional_usd
    if capped_by_leverage:
        position_size_usd = max_notional_usd

    sector_capped = False
    sector_cap_usd: float | None = None
    if sector_limit_pct is not None:
        if sector_limit_pct <= 0:
            raise ValueError("sector_limit_pct must be positive")
        sector_cap_usd = equity_usd * (sector_limit_pct / 100.0)
        available_sector_notional_usd = max(
            0.0, sector_cap_usd - existing_sector_notional_usd
        )
        sector_capped = position_size_usd > available_sector_notional_usd
        if sector_capped:
            position_size_usd = available_sector_notional_usd

    cap_notes = []
    if capped_by_leverage:
        cap_notes.append(f"leverage_notional_cap={max_notional_usd:.2f}")
    if sector_capped:
        cap_notes.append(f"sector_notional_cap={sector_cap_usd:.2f}")
    cap_note = ", " + ", ".join(cap_notes) if cap_notes else ""
    rationale = (
        f"risk={risk_pct:.3f}% of equity (vol_factor={vol_factor:.2f}, corr_factor={corr_factor:.2f}"
        f"{kelly_note}), leverage={leverage}x{cap_note} "
        f"(volatility_percentile={volatility_percentile}, risk_score={risk_score})"
    )
    return PositionSizeResult(
        position_size_usd=round(position_size_usd, 2),
        leverage=leverage,
        risk_amount_usd=round(risk_amount_usd, 2),
        rationale=rationale,
    )
