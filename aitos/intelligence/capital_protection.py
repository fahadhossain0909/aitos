"""Portfolio-level capital protection policies."""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from threading import RLock


@dataclass(frozen=True)
class ProtectionConfig:
    max_portfolio_risk_pct: float = 5.0
    max_correlated_risk_pct: float = 2.5
    max_single_cluster_pct: float = 50.0
    unknown_correlation: float = 0.75
    high_correlation: float = 0.80
    drawdown_1_pct: float = 3.0
    drawdown_2_pct: float = 5.0
    drawdown_3_pct: float = 8.0
    drawdown_stop_pct: float = 10.0
    drawdown_mult_1: float = 0.75
    drawdown_mult_2: float = 0.50
    drawdown_mult_3: float = 0.25
    volatility_mult_high: float = 0.50
    volatility_mult_extreme: float = 0.25
    max_adverse_funding_pct: float = 0.20
    minimum_liquidation_buffer_pct: float = 1.0
    require_liquidation_buffer_for_leverage: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_portfolio_risk_pct",
            "max_correlated_risk_pct",
            "max_single_cluster_pct",
            "drawdown_1_pct",
            "drawdown_2_pct",
            "drawdown_3_pct",
            "drawdown_stop_pct",
            "drawdown_mult_1",
            "drawdown_mult_2",
            "drawdown_mult_3",
            "volatility_mult_high",
            "volatility_mult_extreme",
            "max_adverse_funding_pct",
            "minimum_liquidation_buffer_pct",
        ):
            value = float(getattr(self, name))
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not 0.0 <= self.unknown_correlation <= 1.0:
            raise ValueError("unknown_correlation must be between 0 and 1")
        if not 0.0 <= self.high_correlation <= 1.0:
            raise ValueError("high_correlation must be between 0 and 1")
        if (
            not self.drawdown_1_pct
            <= self.drawdown_2_pct
            <= self.drawdown_3_pct
            <= self.drawdown_stop_pct
        ):
            raise ValueError("drawdown thresholds must be ordered")


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    equity_usd: float
    equity_peak_usd: float
    position_risk_pct: dict[str, float]
    correlations: dict[tuple[str, str], float]
    position_sides: dict[str, str] | None = None

    @property
    def total_risk_pct(self) -> float:
        return sum(max(0.0, float(v)) for v in self.position_risk_pct.values())

    @property
    def drawdown_pct(self) -> float:
        if self.equity_peak_usd <= 0:
            return 100.0
        return max(
            0.0, (self.equity_peak_usd - self.equity_usd) / self.equity_peak_usd * 100.0
        )

    def correlation(self, a: str, b: str, unknown: float) -> float:
        if a == b:
            return 1.0
        value = self.correlations.get((a, b), self.correlations.get((b, a), unknown))
        if not isfinite(value):
            return unknown
        return max(-1.0, min(1.0, value))

    def correlated_risk_pct(
        self,
        positions: dict[str, float] | None = None,
        position_sides: dict[str, str] | None = None,
        unknown_correlation: float = 0.75,
    ) -> float:
        """Portfolio loss-risk volatility using side-adjusted return correlation.

        If either side is unknown, correlation is treated as positively aligned
        by absolute magnitude; missing side metadata never grants hedge credit.
        """
        risks = {
            str(symbol): max(0.0, float(risk))
            for symbol, risk in (positions or self.position_risk_pct).items()
        }
        sides = {
            str(symbol): str(side).upper()
            for symbol, side in (position_sides or self.position_sides or {}).items()
        }
        symbols = sorted(risks)
        variance = sum(risk * risk for risk in risks.values())
        for index, symbol_a in enumerate(symbols):
            for symbol_b in symbols[index + 1 :]:
                correlation = self.correlation(symbol_a, symbol_b, unknown_correlation)
                side_a = sides.get(symbol_a)
                side_b = sides.get(symbol_b)
                if side_a in {"LONG", "SHORT"} and side_b in {"LONG", "SHORT"}:
                    if side_a != side_b:
                        correlation *= -1.0
                else:
                    correlation = abs(correlation)
                variance += 2.0 * correlation * risks[symbol_a] * risks[symbol_b]
        return max(0.0, variance) ** 0.5


@dataclass(frozen=True)
class ProtectionDecision:
    allowed: bool
    risk_multiplier: float
    requested_risk_pct: float
    allowed_risk_pct: float
    correlated_risk_pct: float
    drawdown_pct: float
    reason: str


class PortfolioProtection:
    def __init__(self, config: ProtectionConfig | None = None) -> None:
        self.config = config or ProtectionConfig()

    def drawdown_multiplier(self, drawdown_pct: float) -> float:
        c = self.config
        if drawdown_pct >= c.drawdown_stop_pct:
            return 0.0
        if drawdown_pct >= c.drawdown_3_pct:
            return c.drawdown_mult_3
        if drawdown_pct >= c.drawdown_2_pct:
            return c.drawdown_mult_2
        if drawdown_pct >= c.drawdown_1_pct:
            return c.drawdown_mult_1
        return 1.0

    @staticmethod
    def regime_multiplier(regime: str | None, volatility_score: float | None) -> float:
        volatility = (
            0.0
            if volatility_score is None
            else max(0.0, min(1.0, float(volatility_score)))
        )
        if volatility >= 0.90:
            return 0.25
        if volatility >= 0.75:
            return 0.50
        normalized = (regime or "").lower()
        if normalized in {"high_volatility", "risk_off"}:
            return 0.50
        if normalized == "transition":
            return 0.75
        return 1.0

    def evaluate(
        self,
        *,
        symbol: str,
        requested_risk_pct: float,
        snapshot: PortfolioRiskSnapshot,
        side: str = "LONG",
        regime: str | None = None,
        volatility_score: float | None = None,
        adverse_funding_pct: float = 0.0,
        liquidation_buffer_pct: float | None = None,
        leverage: float = 1.0,
    ) -> ProtectionDecision:
        if (
            not isfinite(float(adverse_funding_pct))
            or adverse_funding_pct < 0
            or not isfinite(float(leverage))
            or leverage <= 0
            or (
                liquidation_buffer_pct is not None
                and (
                    not isfinite(float(liquidation_buffer_pct))
                    or liquidation_buffer_pct < 0
                )
            )
        ):
            return ProtectionDecision(
                False,
                0.0,
                requested_risk_pct,
                0.0,
                snapshot.total_risk_pct,
                snapshot.drawdown_pct,
                "invalid_funding_or_liquidation_input",
            )
        if adverse_funding_pct > self.config.max_adverse_funding_pct:
            return ProtectionDecision(
                False,
                0.0,
                requested_risk_pct,
                0.0,
                snapshot.total_risk_pct,
                snapshot.drawdown_pct,
                "adverse_funding_cost_exceeds_limit",
            )
        if (
            leverage > 1.0
            and self.config.require_liquidation_buffer_for_leverage
            and liquidation_buffer_pct is None
        ):
            return ProtectionDecision(
                False,
                0.0,
                requested_risk_pct,
                0.0,
                snapshot.total_risk_pct,
                snapshot.drawdown_pct,
                "liquidation_buffer_unavailable",
            )
        if (
            leverage > 1.0
            and liquidation_buffer_pct is not None
            and liquidation_buffer_pct < self.config.minimum_liquidation_buffer_pct
        ):
            return ProtectionDecision(
                False,
                0.0,
                requested_risk_pct,
                0.0,
                snapshot.total_risk_pct,
                snapshot.drawdown_pct,
                "liquidation_buffer_below_minimum",
            )
        if requested_risk_pct <= 0 or not isfinite(requested_risk_pct):
            return ProtectionDecision(
                False,
                0.0,
                requested_risk_pct,
                0.0,
                snapshot.total_risk_pct,
                snapshot.drawdown_pct,
                "invalid_requested_risk",
            )
        dd_mult = self.drawdown_multiplier(snapshot.drawdown_pct)
        regime_mult = self.regime_multiplier(regime, volatility_score)
        multiplier = min(dd_mult, regime_mult)
        if multiplier <= 0:
            return ProtectionDecision(
                False,
                0.0,
                requested_risk_pct,
                0.0,
                snapshot.total_risk_pct,
                snapshot.drawdown_pct,
                "drawdown_protection_stop",
            )
        portfolio_remaining = max(
            0.0, self.config.max_portfolio_risk_pct - snapshot.total_risk_pct
        )
        requested_after_protection = min(
            requested_risk_pct * multiplier, portfolio_remaining
        )
        existing_correlated = snapshot.correlated_risk_pct(
            unknown_correlation=self.config.unknown_correlation
        )
        existing_positions = dict(snapshot.position_risk_pct)
        existing_sides = dict(snapshot.position_sides or {})
        candidate_side = str(side).upper()
        if candidate_side not in {"LONG", "SHORT"}:
            return ProtectionDecision(
                False,
                multiplier,
                requested_risk_pct,
                0.0,
                existing_correlated,
                snapshot.drawdown_pct,
                "invalid_position_side",
            )

        existing_variance = existing_correlated**2
        cross_term = 0.0
        for other, risk in existing_positions.items():
            correlation = snapshot.correlation(
                symbol, other, self.config.unknown_correlation
            )
            existing_side = existing_sides.get(other)
            if existing_side in {"LONG", "SHORT"}:
                if existing_side != candidate_side:
                    correlation *= -1.0
            else:
                correlation = abs(correlation)
            cross_term += max(0.0, float(risk)) * correlation

        # Solve x² + 2*cross*x + (existing_variance - cap²) <= 0
        # analytically. A hedge can make risk fall before it rises again, so a
        # monotonic binary search would incorrectly reject valid hedge sizes.
        cap_squared = self.config.max_correlated_risk_pct**2
        constant = existing_variance - cap_squared
        linear = 2.0 * cross_term
        discriminant = linear * linear - 4.0 * constant
        allowed_risk = 0.0
        if constant <= 0.0:
            upper_root = (-linear + max(0.0, discriminant) ** 0.5) / 2.0
            allowed_risk = min(requested_after_protection, max(0.0, upper_root))
        elif discriminant >= 0.0 and linear < 0.0:
            root = discriminant**0.5
            lower_root = (-linear - root) / 2.0
            upper_root = (-linear + root) / 2.0
            if requested_after_protection >= lower_root:
                allowed_risk = min(requested_after_protection, upper_root)
        correlated_risk = (
            max(
                0.0,
                existing_variance + 2.0 * cross_term * allowed_risk + allowed_risk**2,
            )
            ** 0.5
        )
        allowed_risk = max(0.0, round(allowed_risk, 12))
        if allowed_risk <= 0:
            return ProtectionDecision(
                False,
                multiplier,
                requested_risk_pct,
                0.0,
                correlated_risk,
                snapshot.drawdown_pct,
                "portfolio_correlation_or_risk_limit",
            )
        return ProtectionDecision(
            True,
            multiplier,
            requested_risk_pct,
            allowed_risk,
            correlated_risk,
            snapshot.drawdown_pct,
            "approved",
        )


@dataclass(frozen=True)
class Reservation:
    symbol: str
    capital_usd: float
    risk_budget_usd: float


class CapitalReservation:
    """Atomic in-process or SQLite-backed reservation ledger.

    The durable mode uses a database transaction and absolute portfolio
    budgets, so two worker processes cannot both spend the same stale
    availability snapshot. Reservations are leases and expire after a crashed
    worker stops renewing/finishing its authorization window.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        lease_seconds: float = 300.0,
        owner_id: str | None = None,
    ) -> None:
        if not isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        self._lock = RLock()
        self._reservations: dict[str, Reservation] = {}
        if owner_id is not None and not owner_id:
            raise ValueError("reservation owner identity cannot be empty")
        self._owner_id = owner_id or uuid.uuid4().hex
        self._lease_seconds = float(lease_seconds)
        self._conn: sqlite3.Connection | None = None
        if path is not None:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(target), check_same_thread=False, timeout=5.0
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS capital_reservations (
                    symbol TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    capital_usd REAL NOT NULL,
                    risk_budget_usd REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_capital_reservation_expiry "
                "ON capital_reservations(expires_at)"
            )
            self._conn.commit()

    def reserve(
        self,
        reservation: Reservation,
        *,
        available_capital_usd: float,
        available_risk_usd: float,
        capital_limit_usd: float | None = None,
        risk_limit_usd: float | None = None,
    ) -> bool:
        values = (
            reservation.capital_usd,
            reservation.risk_budget_usd,
            available_capital_usd,
            available_risk_usd,
        )
        if any(not isfinite(float(value)) or float(value) < 0 for value in values):
            raise ValueError(
                "reservation and availability values must be finite and non-negative"
            )
        if self._conn is None:
            with self._lock:
                existing = self._reservations.get(reservation.symbol)
                if existing is not None:
                    return existing == reservation
                if reservation.capital_usd > available_capital_usd:
                    return False
                if reservation.risk_budget_usd > available_risk_usd:
                    return False
                self._reservations[reservation.symbol] = reservation
                return True

        if (
            capital_limit_usd is None
            or risk_limit_usd is None
            or not isfinite(float(capital_limit_usd))
            or not isfinite(float(risk_limit_usd))
            or capital_limit_usd < 0
            or risk_limit_usd < 0
        ):
            raise ValueError(
                "durable reservations require finite absolute capital and risk limits"
            )
        now = time.time()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "DELETE FROM capital_reservations WHERE expires_at <= ?",
                    (now,),
                )
                existing = self._conn.execute(
                    "SELECT owner_id, capital_usd, risk_budget_usd "
                    "FROM capital_reservations WHERE symbol = ?",
                    (reservation.symbol.upper(),),
                ).fetchone()
                if existing is not None:
                    self._conn.rollback()
                    return (
                        existing[0] == self._owner_id
                        and float(existing[1]) == reservation.capital_usd
                        and float(existing[2]) == reservation.risk_budget_usd
                    )
                totals = self._conn.execute(
                    """
                    SELECT COALESCE(SUM(capital_usd), 0),
                           COALESCE(SUM(risk_budget_usd), 0)
                    FROM capital_reservations
                    WHERE symbol != ?
                    """,
                    (reservation.symbol.upper(),),
                ).fetchone()
                total_capital = float(totals[0]) + reservation.capital_usd
                total_risk = float(totals[1]) + reservation.risk_budget_usd
                if (
                    total_capital > float(capital_limit_usd) + 1e-9
                    or total_risk > float(risk_limit_usd) + 1e-9
                ):
                    self._conn.rollback()
                    return False
                self._conn.execute(
                    """
                    INSERT INTO capital_reservations
                    (symbol, owner_id, capital_usd, risk_budget_usd, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        capital_usd = excluded.capital_usd,
                        risk_budget_usd = excluded.risk_budget_usd,
                        expires_at = excluded.expires_at
                    """,
                    (
                        reservation.symbol.upper(),
                        self._owner_id,
                        reservation.capital_usd,
                        reservation.risk_budget_usd,
                        now + self._lease_seconds,
                    ),
                )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def release(self, symbol: str) -> Reservation | None:
        with self._lock:
            if self._conn is None:
                return self._reservations.pop(symbol, None)
            row = self._conn.execute(
                """
                SELECT capital_usd, risk_budget_usd
                FROM capital_reservations
                WHERE symbol = ? AND owner_id = ?
                """,
                (symbol.upper(), self._owner_id),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM capital_reservations WHERE symbol = ? AND owner_id = ?",
                (symbol.upper(), self._owner_id),
            )
            self._conn.commit()
            return Reservation(symbol.upper(), float(row[0]), float(row[1]))

    def restore_confirmed_exposure(self, reservation: Reservation) -> None:
        """Account-matched restored exposure is retained, never re-authorized.

        Caller must hold exclusive live-runtime ownership and verify the full
        account snapshot first. Existing larger reservations remain conservative.
        """
        if (
            any(
                not isfinite(value) or value < 0
                for value in (reservation.capital_usd, reservation.risk_budget_usd)
            )
            or not reservation.symbol
        ):
            raise ValueError("invalid confirmed exposure reservation")
        with self._lock:
            if self._conn is None:
                old = self._reservations.get(reservation.symbol)
                self._reservations[reservation.symbol] = Reservation(
                    reservation.symbol,
                    max(reservation.capital_usd, old.capital_usd if old else 0),
                    max(reservation.risk_budget_usd, old.risk_budget_usd if old else 0),
                )
                return
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                old = self._conn.execute(
                    "SELECT owner_id FROM capital_reservations WHERE symbol=?",
                    (reservation.symbol.upper(),),
                ).fetchone()
                if old is not None and old[0] != self._owner_id:
                    raise ValueError("restored reservation belongs to another owner")
                self._conn.execute(
                    "INSERT INTO capital_reservations VALUES (?,?,?,?,?) "
                    "ON CONFLICT(symbol) DO UPDATE SET "
                    "capital_usd=MAX(capital_usd,excluded.capital_usd), "
                    "risk_budget_usd=MAX(risk_budget_usd,excluded.risk_budget_usd), "
                    "expires_at=excluded.expires_at",
                    (
                        reservation.symbol.upper(),
                        self._owner_id,
                        reservation.capital_usd,
                        reservation.risk_budget_usd,
                        float("inf"),
                    ),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def pin(self, symbol: str) -> bool:
        """Retain owned capital until explicit reconciliation/release, not TTL.

        Authorization-only leases still expire. Submitted/open exposure does
        not become spendable merely because a worker died or a timer elapsed.
        """
        with self._lock:
            if self._conn is None:
                return symbol in self._reservations
            cursor = self._conn.execute(
                "UPDATE capital_reservations SET expires_at=? "
                "WHERE symbol=? AND owner_id=? AND expires_at>?",
                (float("inf"), symbol.upper(), self._owner_id, time.time()),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def renew(self, symbol: str) -> bool:
        """Extend an open-position reservation lease owned by this runtime."""
        with self._lock:
            if self._conn is None:
                return symbol in self._reservations
            cursor = self._conn.execute(
                """
                UPDATE capital_reservations
                SET expires_at = MAX(expires_at, ?)
                WHERE symbol = ? AND owner_id = ? AND expires_at > ?
                """,
                (
                    time.time() + self._lease_seconds,
                    symbol.upper(),
                    self._owner_id,
                    time.time(),
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def get(self, symbol: str) -> Reservation | None:
        with self._lock:
            if self._conn is None:
                return self._reservations.get(symbol)
            row = self._conn.execute(
                """
                SELECT capital_usd, risk_budget_usd
                FROM capital_reservations
                WHERE symbol = ? AND owner_id = ? AND expires_at > ?
                """,
                (symbol.upper(), self._owner_id, time.time()),
            ).fetchone()
            return (
                Reservation(symbol.upper(), float(row[0]), float(row[1]))
                if row is not None
                else None
            )

    @property
    def reserved_capital_usd(self) -> float:
        with self._lock:
            if self._conn is None:
                return sum(item.capital_usd for item in self._reservations.values())
            return float(
                self._conn.execute(
                    "SELECT COALESCE(SUM(capital_usd), 0) FROM capital_reservations "
                    "WHERE expires_at > ?",
                    (time.time(),),
                ).fetchone()[0]
            )

    @property
    def reserved_risk_usd(self) -> float:
        with self._lock:
            if self._conn is None:
                return sum(item.risk_budget_usd for item in self._reservations.values())
            return float(
                self._conn.execute(
                    "SELECT COALESCE(SUM(risk_budget_usd), 0) FROM capital_reservations "
                    "WHERE expires_at > ?",
                    (time.time(),),
                ).fetchone()[0]
            )

    def snapshot(self) -> tuple[Reservation, ...]:
        with self._lock:
            if self._conn is None:
                return tuple(self._reservations.values())
            rows = self._conn.execute(
                """
                SELECT symbol, capital_usd, risk_budget_usd
                FROM capital_reservations WHERE expires_at > ?
                ORDER BY symbol
                """,
                (time.time(),),
            ).fetchall()
            return tuple(
                Reservation(str(row[0]), float(row[1]), float(row[2])) for row in rows
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
