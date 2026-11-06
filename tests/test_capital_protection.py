import multiprocessing
import time

from aitos.intelligence.capital_protection import (
    CapitalReservation,
    PortfolioProtection,
    PortfolioRiskSnapshot,
    Reservation,
)


def snapshot(**kwargs):
    return PortfolioRiskSnapshot(
        equity_usd=10_000.0,
        equity_peak_usd=kwargs.get("peak", 10_000.0),
        position_risk_pct=kwargs.get("positions", {}),
        correlations=kwargs.get("correlations", {}),
        position_sides=kwargs.get("sides"),
    )


def test_drawdown_reduces_risk_and_stops_at_limit():
    gate = PortfolioProtection()
    assert gate.drawdown_multiplier(4.0) == 0.75
    assert gate.drawdown_multiplier(6.0) == 0.50
    assert gate.drawdown_multiplier(9.0) == 0.25
    assert gate.drawdown_multiplier(10.0) == 0.0


def test_high_volatility_reduces_requested_risk():
    decision = PortfolioProtection().evaluate(
        symbol="BTCUSDT",
        requested_risk_pct=1.0,
        snapshot=snapshot(),
        regime="high_volatility",
        volatility_score=0.80,
    )
    assert decision.allowed
    assert decision.allowed_risk_pct == 0.50
    assert decision.risk_multiplier == 0.50


def test_correlated_existing_risk_is_not_treated_as_zero():
    decision = PortfolioProtection().evaluate(
        symbol="ETHUSDT",
        requested_risk_pct=1.0,
        snapshot=snapshot(
            positions={"BTCUSDT": 2.0},
            correlations={("BTCUSDT", "ETHUSDT"): 0.95},
        ),
    )
    assert decision.allowed
    assert decision.allowed_risk_pct <= 0.60
    assert decision.correlated_risk_pct > 2.0


def test_unknown_correlation_is_conservative():
    decision = PortfolioProtection().evaluate(
        symbol="SOLUSDT",
        requested_risk_pct=1.0,
        snapshot=snapshot(positions={"BTCUSDT": 2.0}),
    )
    assert decision.allowed
    assert decision.allowed_risk_pct <= 1.0
    assert decision.correlated_risk_pct >= 1.5


def test_drawdown_stop_is_hard_veto():
    decision = PortfolioProtection().evaluate(
        symbol="BTCUSDT",
        requested_risk_pct=1.0,
        snapshot=snapshot(peak=11_500.0),
    )
    assert not decision.allowed
    assert decision.reason == "drawdown_protection_stop"


def test_reservation_prevents_oversubscription_and_is_idempotent():
    ledger = CapitalReservation()
    first = Reservation("BTCUSDT", 6_000.0, 60.0)
    second = Reservation("ETHUSDT", 5_000.0, 50.0)
    assert ledger.reserve(
        first, available_capital_usd=10_000.0, available_risk_usd=100.0
    )
    assert not ledger.reserve(
        second, available_capital_usd=4_000.0, available_risk_usd=40.0
    )
    assert ledger.reserve(first, available_capital_usd=4_000.0, available_risk_usd=40.0)
    assert ledger.reserved_capital_usd == 6_000.0
    assert ledger.release("BTCUSDT") == first
    assert ledger.release("BTCUSDT") is None
    assert ledger.reserved_capital_usd == 0.0


def test_opposite_direction_position_receives_correlation_hedge_credit():
    decision = PortfolioProtection().evaluate(
        symbol="ETHUSDT",
        side="SHORT",
        requested_risk_pct=1.0,
        snapshot=snapshot(
            positions={"BTCUSDT": 2.0},
            correlations={("BTCUSDT", "ETHUSDT"): 0.95},
            sides={"BTCUSDT": "LONG"},
        ),
    )
    assert decision.allowed
    assert decision.allowed_risk_pct == 1.0
    assert decision.correlated_risk_pct < 1.5


def test_negative_asset_correlation_increases_opposite_side_pnl_risk():
    decision = PortfolioProtection().evaluate(
        symbol="ETHUSDT",
        side="SHORT",
        requested_risk_pct=1.0,
        snapshot=snapshot(
            positions={"BTCUSDT": 2.0},
            correlations={("BTCUSDT", "ETHUSDT"): -0.95},
            sides={"BTCUSDT": "LONG"},
        ),
    )
    assert decision.allowed
    assert decision.allowed_risk_pct < 0.60


def test_missing_existing_side_metadata_never_grants_hedge_credit():
    decision = PortfolioProtection().evaluate(
        symbol="ETHUSDT",
        side="SHORT",
        requested_risk_pct=1.0,
        snapshot=snapshot(
            positions={"BTCUSDT": 2.0},
            correlations={("BTCUSDT", "ETHUSDT"): 0.95},
        ),
    )
    assert decision.allowed
    assert decision.allowed_risk_pct < 0.60


def test_candidate_hedge_can_reduce_existing_correlated_risk_above_limit():
    decision = PortfolioProtection().evaluate(
        symbol="ETHUSDT",
        side="SHORT",
        requested_risk_pct=1.0,
        snapshot=snapshot(
            positions={"BTCUSDT": 3.0},
            correlations={("BTCUSDT", "ETHUSDT"): 0.95},
            sides={"BTCUSDT": "LONG"},
        ),
    )
    assert decision.allowed
    assert decision.allowed_risk_pct == 1.0
    assert decision.correlated_risk_pct < 2.5


def test_sqlite_reservations_prevent_cross_instance_oversubscription(tmp_path):
    path = tmp_path / "reservations.sqlite3"
    first = CapitalReservation(path=path)
    second = CapitalReservation(path=path)
    limits = {"capital_limit_usd": 10_000.0, "risk_limit_usd": 100.0}
    try:
        assert first.reserve(
            Reservation("BTCUSDT", 6_000.0, 60.0),
            available_capital_usd=10_000.0,
            available_risk_usd=100.0,
            **limits,
        )
        # A second process cannot reserve the same symbol under another owner.
        assert not second.reserve(
            Reservation("BTCUSDT", 6_000.0, 60.0),
            available_capital_usd=10_000.0,
            available_risk_usd=100.0,
            **limits,
        )
        # Nor can it rely on a stale availability snapshot for another symbol.
        assert not second.reserve(
            Reservation("ETHUSDT", 5_000.0, 50.0),
            available_capital_usd=10_000.0,
            available_risk_usd=100.0,
            **limits,
        )
        assert first.reserved_capital_usd == 6_000.0
        assert first.release("BTCUSDT") == Reservation("BTCUSDT", 6_000.0, 60.0)
        assert second.reserve(
            Reservation("ETHUSDT", 5_000.0, 50.0),
            available_capital_usd=10_000.0,
            available_risk_usd=100.0,
            **limits,
        )
        assert second.reserved_risk_usd == 50.0
    finally:
        first.close()
        second.close()


def test_adverse_funding_cost_is_a_hard_veto():
    decision = PortfolioProtection().evaluate(
        symbol="BTCUSDT",
        requested_risk_pct=0.5,
        snapshot=snapshot(),
        adverse_funding_pct=0.21,
    )
    assert not decision.allowed
    assert decision.reason == "adverse_funding_cost_exceeds_limit"


def test_leveraged_position_requires_a_safe_liquidation_buffer():
    gate = PortfolioProtection()
    unavailable = gate.evaluate(
        symbol="BTCUSDT",
        requested_risk_pct=0.5,
        snapshot=snapshot(),
        leverage=5.0,
    )
    too_close = gate.evaluate(
        symbol="BTCUSDT",
        requested_risk_pct=0.5,
        snapshot=snapshot(),
        leverage=5.0,
        liquidation_buffer_pct=0.5,
    )
    safe = gate.evaluate(
        symbol="BTCUSDT",
        requested_risk_pct=0.5,
        snapshot=snapshot(),
        leverage=5.0,
        liquidation_buffer_pct=2.0,
    )
    assert not unavailable.allowed
    assert unavailable.reason == "liquidation_buffer_unavailable"
    assert not too_close.allowed
    assert too_close.reason == "liquidation_buffer_below_minimum"
    assert safe.allowed


def _reserve_from_child_process(path, symbol, result_queue):
    ledger = CapitalReservation(path=path, lease_seconds=30.0)
    try:
        accepted = ledger.reserve(
            Reservation(symbol, 20.0, 2.0),
            available_capital_usd=100.0,
            available_risk_usd=10.0,
            capital_limit_usd=100.0,
            risk_limit_usd=10.0,
        )
        result_queue.put(bool(accepted))
    finally:
        ledger.close()


def test_sqlite_reservations_survive_real_multiprocess_contention(tmp_path):
    path = str(tmp_path / "multiprocess-reservations.sqlite3")
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_reserve_from_child_process,
            args=(path, f"SYM{index}USDT", result_queue),
        )
        for index in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15.0)
        assert process.exitcode == 0
    accepted = [result_queue.get(timeout=2.0) for _ in processes]
    assert sum(accepted) == 5
    ledger = CapitalReservation(path=path)
    try:
        assert ledger.reserved_capital_usd == 100.0
        assert ledger.reserved_risk_usd == 10.0
    finally:
        ledger.close()


def test_crashed_worker_reservation_expires_after_lease(tmp_path):
    ledger = CapitalReservation(
        path=tmp_path / "lease-expiry.sqlite3", lease_seconds=0.05
    )
    try:
        assert ledger.reserve(
            Reservation("BTCUSDT", 50.0, 5.0),
            available_capital_usd=100.0,
            available_risk_usd=10.0,
            capital_limit_usd=100.0,
            risk_limit_usd=10.0,
        )
        time.sleep(0.08)
        replacement = CapitalReservation(
            path=tmp_path / "lease-expiry.sqlite3", lease_seconds=1.0
        )
        try:
            assert replacement.reserve(
                Reservation("ETHUSDT", 100.0, 10.0),
                available_capital_usd=100.0,
                available_risk_usd=10.0,
                capital_limit_usd=100.0,
                risk_limit_usd=10.0,
            )
        finally:
            replacement.close()
    finally:
        ledger.close()
