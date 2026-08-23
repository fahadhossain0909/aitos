"""Structured AMT engine built on volume profile, explicit TPO and L2 data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, Sequence

from aitos.models.market import Kline, OrderBookSnapshot, TradeTick

from .tpo_profile import TPOObservation, TPOProfile, build_tpo_profile
from .volume_profile import VolumeProfile, build_volume_profile


class AuctionState(str, Enum):
    BALANCE = "balance"
    DISCOVERY_UP = "discovery_up"
    DISCOVERY_DOWN = "discovery_down"
    ACCEPTANCE = "acceptance"
    REJECTION = "rejection"
    ROTATION = "rotation"
    TREND = "trend"
    UNKNOWN = "unknown"


class DayType(str, Enum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    TREND = "trend"
    DOUBLE_DISTRIBUTION = "double_distribution"
    NEUTRAL = "neutral"


class ValueMigration(str, Enum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AMTContext:
    profile: VolumeProfile
    state: AuctionState
    day_type: DayType
    value_migration: ValueMigration
    acceptance: float
    rejection: float
    price_location: float
    ib_high: float
    ib_low: float
    confidence: float
    rationale: tuple[str, ...]
    ib_range: float = 0.0
    ib_extension_up: float = 0.0
    ib_extension_down: float = 0.0
    open_price: float = 0.0
    open_location: str = "unknown"
    book_imbalance: float = 0.0
    data_quality: float = 0.0
    tpo: TPOProfile | None = None
    tpo_value_alignment: float = 0.0

    @property
    def poc(self) -> float:
        return self.profile.poc

    @property
    def vah(self) -> float:
        return self.profile.vah

    @property
    def val(self) -> float:
        return self.profile.val


class AMTEngine:
    """Deterministic AMT context builder.

    Volume profile is authoritative for trade-volume distribution. TPO is an
    optional, independent time-at-price measurement and is only produced when
    explicit observations are supplied. For crypto, callers should explicitly
    select the session convention rather than silently assuming a market day.
    """

    def __init__(
        self,
        tick_size: float,
        value_area_pct: float = 0.70,
        ib_minutes: int = 60,
        acceptance_window: int = 50,
        rejection_window: int = 10,
        tpo_bracket_minutes: int = 30,
    ) -> None:
        if tick_size <= 0 or not 0 < value_area_pct <= 1:
            raise ValueError("invalid tick_size/value_area_pct")
        if (
            ib_minutes <= 0
            or acceptance_window <= 0
            or rejection_window <= 0
            or tpo_bracket_minutes <= 0
        ):
            raise ValueError("session/window parameters must be > 0")
        self.tick_size, self.value_area_pct = tick_size, value_area_pct
        self.ib_minutes, self.acceptance_window, self.rejection_window = (
            ib_minutes,
            acceptance_window,
            rejection_window,
        )
        self.tpo_bracket_minutes = tpo_bracket_minutes

    @staticmethod
    def _session_start(ts: datetime) -> datetime:
        return ts.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    @staticmethod
    def _open_location(open_price: float, profile: VolumeProfile) -> str:
        if not open_price or not profile.bins:
            return "unknown"
        if open_price > profile.vah:
            return "above_value"
        if open_price < profile.val:
            return "below_value"
        return (
            "inside_value_upper" if open_price >= profile.poc else "inside_value_lower"
        )

    @staticmethod
    def _tpo_alignment(profile: VolumeProfile, tpo: TPOProfile | None) -> float:
        if tpo is None or not profile.bins:
            return 0.0
        scale = max(profile.vah - profile.val, tpo.vah - tpo.val, 0.0)
        if scale == 0:
            return 1.0 if profile.poc == tpo.poc else 0.0
        poc_agreement = max(0.0, 1.0 - abs(profile.poc - tpo.poc) / scale)
        value_overlap = (
            max(0.0, min(profile.vah, tpo.vah) - max(profile.val, tpo.val)) / scale
        )
        return round(min(1.0, 0.6 * poc_agreement + 0.4 * value_overlap), 4)

    def analyze(
        self,
        trades: Iterable[TradeTick],
        klines: Sequence[Kline] | None = None,
        book: OrderBookSnapshot | None = None,
        previous_profile: VolumeProfile | None = None,
        session_start: datetime | None = None,
        tpo_observations: Iterable[TPOObservation] | None = None,
    ) -> AMTContext:
        ticks = sorted(
            (t for t in trades if t.quantity > 0 and t.price > 0),
            key=lambda t: t.timestamp,
        )
        profile = build_volume_profile(ticks, self.tick_size, self.value_area_pct)
        if not ticks:
            return AMTContext(
                profile,
                AuctionState.UNKNOWN,
                DayType.UNKNOWN,
                ValueMigration.UNKNOWN,
                0,
                0,
                0,
                0,
                0,
                0,
                ("no valid trades",),
                data_quality=0,
            )
        if session_start is None:
            session_start = self._session_start(ticks[0].timestamp)
        session_start = session_start.astimezone(timezone.utc)
        session_end, ib_end = session_start + timedelta(
            days=1
        ), session_start + timedelta(minutes=self.ib_minutes)
        session_ticks = [
            t
            for t in ticks
            if session_start <= t.timestamp.astimezone(timezone.utc) < session_end
        ] or ticks
        ib_ticks = [
            t for t in session_ticks if t.timestamp.astimezone(timezone.utc) < ib_end
        ]
        last, open_price = session_ticks[-1].price, session_ticks[0].price
        width = profile.vah - profile.val
        location = max(0, min(1, (last - profile.val) / width)) if width > 0 else 0.5
        ib_high, ib_low = max((t.price for t in ib_ticks), default=0), min(
            (t.price for t in ib_ticks), default=0
        )
        ib_range = max(0, ib_high - ib_low)
        ib_up, ib_down = max(0, last - ib_high) if ib_high else 0, (
            max(0, ib_low - last) if ib_low else 0
        )
        recent = session_ticks[-min(len(session_ticks), self.acceptance_window) :]
        inside = sum(profile.val <= t.price <= profile.vah for t in recent)
        acceptance = inside / len(recent) if recent else 0
        tail = recent[-min(len(recent), self.rejection_window) :]
        outside_tail = sum(t.price > profile.vah or t.price < profile.val for t in tail)
        returned = sum(profile.val <= t.price <= profile.vah for t in tail)
        rejection = returned / len(tail) if outside_tail else 0
        migration = (
            ValueMigration.UNKNOWN
            if previous_profile is None
            else (
                ValueMigration.UP
                if profile.poc > previous_profile.poc + self.tick_size
                else (
                    ValueMigration.DOWN
                    if profile.poc < previous_profile.poc - self.tick_size
                    else ValueMigration.FLAT
                )
            )
        )
        outside_strength = abs(last - profile.poc) / width if width > 0 else 0
        if (
            last > profile.vah
            and migration == ValueMigration.UP
            and acceptance < 0.75
            or last < profile.val
            and migration == ValueMigration.DOWN
            and acceptance < 0.75
        ):
            state = AuctionState.TREND
        elif (
            (last > profile.vah or last < profile.val)
            and outside_tail
            and rejection < 0.3
        ):
            state = AuctionState.ACCEPTANCE
        elif rejection >= 0.3:
            state = AuctionState.REJECTION
        elif acceptance >= 0.75 and outside_strength < 0.75:
            state = AuctionState.BALANCE
        elif last > profile.vah:
            state = AuctionState.DISCOVERY_UP
        elif last < profile.val:
            state = AuctionState.DISCOVERY_DOWN
        else:
            state = AuctionState.ROTATION
        day_type = self._classify_day_type(profile, state)
        book_imbalance = 0.0
        if book is not None:
            bid, ask = sum(q for _, q in book.bids), sum(q for _, q in book.asks)
            if bid + ask > 0:
                book_imbalance = (bid - ask) / (bid + ask)
        tpo = None
        if tpo_observations is not None:
            obs = [
                o
                for o in tpo_observations
                if session_start <= o.timestamp.astimezone(timezone.utc) < session_end
            ]
            if obs:
                tpo = build_tpo_profile(
                    obs, self.tick_size, self.tpo_bracket_minutes, self.value_area_pct
                )
        alignment = self._tpo_alignment(profile, tpo)
        data_quality = min(
            1,
            0.4
            + min(0.4, len(session_ticks) / 1000)
            + (0.1 if book is not None else 0)
            + (0.1 if tpo is not None else 0),
        )
        confidence = min(
            1,
            0.25
            + 0.30 * data_quality
            + 0.20 * max(acceptance, rejection)
            + 0.15 * min(1, abs(book_imbalance))
            + 0.10 * alignment,
        )
        rationale = [
            f"state={state.value}",
            f"day_type={day_type.value}",
            f"poc={profile.poc:g}",
            f"vah={profile.vah:g}",
            f"val={profile.val:g}",
            f"value_migration={migration.value}",
            f"acceptance={acceptance:.3f}",
            f"rejection={rejection:.3f}",
            f"open_location={self._open_location(open_price, profile)}",
        ]
        if ib_high:
            rationale.append(f"initial_balance={ib_low:g}-{ib_high:g}")
        if book is not None:
            rationale.append(f"book_imbalance={book_imbalance:.3f}")
        if tpo is not None:
            rationale.append(
                f"tpo_poc={tpo.poc:g};tpo_value={tpo.val:g}-{tpo.vah:g};tpo_alignment={alignment:.3f}"
            )
        return AMTContext(
            profile,
            state,
            day_type,
            migration,
            round(acceptance, 4),
            round(rejection, 4),
            round(location, 4),
            ib_high,
            ib_low,
            round(confidence, 4),
            tuple(rationale),
            ib_range,
            ib_up,
            ib_down,
            open_price,
            self._open_location(open_price, profile),
            round(book_imbalance, 4),
            round(data_quality, 4),
            tpo,
            alignment,
        )

    @staticmethod
    def _classify_day_type(profile: VolumeProfile, state: AuctionState) -> DayType:
        if state == AuctionState.TREND:
            return DayType.TREND
        if len(profile.bins) < 5 or profile.total_volume <= 0:
            return DayType.UNKNOWN
        values = [v for _, v in profile.bins]
        mean = sum(values) / len(values)
        if mean <= 0:
            return DayType.UNKNOWN
        peaks = [
            i
            for i in range(1, len(values) - 1)
            if values[i] > values[i - 1]
            and values[i] >= values[i + 1]
            and values[i] >= mean * 1.5
        ]
        if len(peaks) >= 2:
            valley = min(values[peaks[0] : peaks[-1] + 1])
            floor = min(values[peaks[0]], values[peaks[-1]])
            if floor > 0 and valley / floor <= 0.55:
                return DayType.DOUBLE_DISTRIBUTION
        return (
            DayType.NORMAL
            if max(values) / profile.total_volume < 0.25
            else DayType.NEUTRAL
        )
