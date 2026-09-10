from types import SimpleNamespace

from aitos.intelligence.position_runtime import (
    MAX_DEEP_SYMBOLS,
    MAX_OPEN_POSITIONS,
    POSITION_CAPITAL_POOL_PCT,
    POSITION_DATA_RESERVE_PCT,
    _capital_policy_consensus,
    _merge_symbols,
)


def test_position_policy_is_five_slots_with_reserve_buffer():
    assert MAX_OPEN_POSITIONS == 5
    assert POSITION_DATA_RESERVE_PCT == 20.0
    assert POSITION_CAPITAL_POOL_PCT == 80.0
    assert MAX_DEEP_SYMBOLS == 6


def test_merge_symbols_preserves_requested_order_and_adds_open_positions():
    merged = _merge_symbols(
        ["BTCUSDT", "ETHUSDT", "BTCUSDT"],
        ["SOLUSDT", "ETHUSDT", "XRPUSDT"],
    )
    assert merged == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def test_capital_policy_reports_five_slot_pool_and_remaining_capacity():
    portfolio = SimpleNamespace(
        equity_usd=10_000.0,
        positions=(
            SimpleNamespace(notional_usd=1_000.0, symbol="BTCUSDT"),
            SimpleNamespace(notional_usd=500.0, symbol="ETHUSDT"),
        ),
    )
    policy = _capital_policy_consensus(portfolio)
    assert policy["capital_pool_usd"] == 8_000.0
    assert policy["deployed_notional_usd"] == 1_500.0
    assert policy["remaining_policy_capacity_usd"] == 6_500.0
    assert policy["per_slot_capital_usd"] == 1_600.0
