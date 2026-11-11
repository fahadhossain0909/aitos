from decimal import Decimal
from types import SimpleNamespace

import pytest

from aitos.security.risk_limits import AccountRiskSnapshot
from aitos.security.signing_policy import IntentRejected
from aitos.security.risk_state import SQLiteSignerRiskState


NOW = 1_800_000_000_000


class Evidence:
    def get_snapshot(self, account_alias, *, now_ms):
        return AccountRiskSnapshot(
            account_alias=account_alias,
            captured_at_ms=now_ms,
            gross_exposure=Decimal("100"),
            symbol_exposure={"BTCUSDT": Decimal("50")},
            daily_loss=Decimal("5"),
            order_count_window=0,
            positions={"BTCUSDT": Decimal("0.01")},
        )


def test_order_window_is_durable_and_atomically_enforced(tmp_path):
    path = tmp_path / "risk.sqlite3"
    first = SQLiteSignerRiskState(
        upstream=Evidence(),
        database_path=path,
        max_orders_per_window=2,
        window_ms=1000,
    )
    assert first.get_snapshot("paper-primary", now_ms=NOW).order_count_window == 0
    assert first.get_snapshot("paper-primary", now_ms=NOW + 1).order_count_window == 1
    with pytest.raises(IntentRejected, match="order_rate_limit_reached"):
        first.get_snapshot("paper-primary", now_ms=NOW + 2)
    first.close()

    reopened = SQLiteSignerRiskState(
        upstream=Evidence(),
        database_path=path,
        max_orders_per_window=2,
        window_ms=1000,
    )
    with pytest.raises(IntentRejected, match="order_rate_limit_reached"):
        reopened.get_snapshot("paper-primary", now_ms=NOW + 3)
    assert (
        reopened.get_snapshot("paper-primary", now_ms=NOW + 1000).order_count_window
        == 0
    )
    reopened.close()


def test_realized_net_loss_is_idempotent_and_survives_restart(tmp_path):
    path = tmp_path / "risk.sqlite3"
    first = SQLiteSignerRiskState(
        upstream=Evidence(), database_path=path, max_orders_per_window=10
    )
    assert first.record_realized_pnl(
        "paper-primary", "trade-001", Decimal("-12.50"), occurred_at_ms=NOW
    )
    assert not first.record_realized_pnl(
        "paper-primary", "trade-001", Decimal("-12.50"), occurred_at_ms=NOW
    )
    assert first.record_realized_pnl(
        "paper-primary", "trade-002", Decimal("2.50"), occurred_at_ms=NOW
    )
    first.close()

    reopened = SQLiteSignerRiskState(
        upstream=Evidence(), database_path=path, max_orders_per_window=10
    )
    snapshot = reopened.get_snapshot("paper-primary", now_ms=NOW)
    assert snapshot.daily_loss == Decimal("10.00")
    reopened.close()


def test_evidence_provider_mismatch_fails_closed(tmp_path):
    class WrongAccount(Evidence):
        def get_snapshot(self, account_alias, *, now_ms):
            return super().get_snapshot("another-account", now_ms=now_ms)

    with SQLiteSignerRiskState(
        upstream=WrongAccount(),
        database_path=tmp_path / "risk.sqlite3",
        max_orders_per_window=10,
    ) as state:
        with pytest.raises(IntentRejected, match="account_evidence_mismatch"):
            state.get_snapshot("paper-primary", now_ms=NOW)



def test_reduce_only_snapshot_does_not_consume_growth_order_budget(tmp_path):
    with SQLiteSignerRiskState(
        upstream=Evidence(),
        database_path=tmp_path / "risk.sqlite3",
        max_orders_per_window=1,
        window_ms=1000,
    ) as state:
        assert state.get_snapshot("paper-primary", now_ms=NOW).order_count_window == 0
        exit_intent = SimpleNamespace(
            account_alias="paper-primary",
            reduce_only=True,
        )
        assert (
            state.get_snapshot_for_intent(exit_intent, now_ms=NOW + 1).order_count_window
            == 1
        )
        with pytest.raises(IntentRejected, match="order_rate_limit_reached"):
            state.get_snapshot("paper-primary", now_ms=NOW + 2)
