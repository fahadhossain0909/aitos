"""Financial evidence contracts, not a profitability or significance certificate."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from aitos.trading.performance import analyze_closed_trades
from aitos.trading.persistent_state import DurableTradingStateStore


def row(i=0, pnl=10, **kw):
    return dict(
        trade_id=f"t{i}",
        symbol="BTCUSDT",
        side="LONG",
        state="position_closed",
        pnl=pnl,
        entry_time="2026-11-01T00:00:00Z",
        exit_time=f"2026-11-{i+2:02}T12:00:00Z",
        fees_paid_usd=1,
        funding_pnl_usd=-0.1,
        strategy_id="frozen",
        **kw,
    )


def test_metrics_are_net_recorded_pnl_not_double_charged_costs():
    result = analyze_closed_trades([row(0, 100), row(1, -50), row(2, 0), row(3, 25)])
    s = result["summary"]
    assert s["net_pnl_usd"] == 75
    assert s["wins"] == 2 and s["losses"] == 1 and s["breakeven"] == 1
    assert s["win_rate"] == 0.5 and s["expectancy_usd"] == 18.75
    assert s["profit_factor"] == 2.5
    assert s["max_closed_pnl_drawdown_usd"] == 50
    assert result["curve"][-1]["net_pnl_usd"] == 75
    assert result["costs"]["fees_paid_usd"]["total_usd"] == 4
    assert result["selection"] == "latest_ledger_updates"


def test_equal_close_timestamps_are_grouped_before_drawdown():
    rows = [row(0, 100), row(1, -80)]
    rows[1]["exit_time"] = rows[0]["exit_time"]
    report = analyze_closed_trades(rows)
    assert len(report["curve"]) == 1
    assert report["summary"]["max_closed_pnl_drawdown_usd"] == 0


def test_no_losses_is_not_infinite_profit_factor():
    report = analyze_closed_trades([row()])
    assert report["summary"]["profit_factor"] is None
    assert "undefined" in report["summary"]["profit_factor_reason"]


def test_missing_cost_fields_stay_unknown():
    r = row()
    del r["fees_paid_usd"]
    costs = analyze_closed_trades([r])["costs"]["fees_paid_usd"]
    assert costs == dict(observed_rows=0, total_rows=1, total_usd=None)


def test_empty_window_is_not_zero_account_pnl():
    report = analyze_closed_trades([])
    assert report["summary"]["net_pnl_usd"] is None
    assert report["summary"]["max_closed_pnl_drawdown_usd"] is None
    assert report["curve"] == []


def test_reproducible_hash_is_order_independent_and_ignores_secret_fields():
    rows = [row(0), row(1, -5)]
    before = copy.deepcopy(rows)
    a = analyze_closed_trades(rows)
    rows[0]["api_secret"] = "never included"
    b = analyze_closed_trades(rows[::-1])
    assert a["evidence_sha256"] == b["evidence_sha256"]
    assert "never included" not in json.dumps(b)
    assert before[0]["pnl"] == rows[0]["pnl"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("pnl", None),
        ("pnl", True),
        ("pnl", float("nan")),
        ("pnl", "12"),
        ("pnl", float("inf")),
        ("side", "SIDEWAYS"),
        ("state", "position_opened"),
        ("fees_paid_usd", float("nan")),
        ("exit_time", "2026-11-02T12:00:00"),
        ("exit_time", "2020-01-01T00:00:00Z"),
    ],
)
def test_malformed_evidence_invalidates_instead_of_disappearing(field, value):
    r = row()
    r[field] = value
    with pytest.raises(ValueError):
        analyze_closed_trades([r])


def test_duplicates_and_bounds_are_rejected():
    with pytest.raises(ValueError):
        analyze_closed_trades([row(), row()])
    with pytest.raises(ValueError):
        analyze_closed_trades([row()], window_limit=0)


def test_derived_overflow_is_not_serialized_as_an_infinite_ratio():
    with pytest.raises((ValueError, OverflowError)):
        analyze_closed_trades([row(0, 1e308), row(1, -1e-308)])


def make_store(rows):
    query = AsyncMock(return_value=SimpleNamespace(result_rows=rows))
    return (
        DurableTradingStateStore(SimpleNamespace(_client=SimpleNamespace(query=query))),
        query,
    )


async def test_performance_is_one_bounded_query_with_truncation_evidence():
    rows = [row(0), row(1, -5)]
    store, query = make_store([(r["trade_id"], json.dumps(r), 1) for r in rows])
    report = await store.query_closed_performance(limit=1)
    assert query.await_count == 1
    assert query.call_args.kwargs["parameters"] == {"limit": 2}
    assert report["truncated"] and report["loaded_trades"] == 1


@pytest.mark.parametrize(
    "bad",
    [
        ("t", "{}", 2),
        ("t", "{}", 0),
        ("t", "bad", 1),
        ("t", json.dumps(dict(trade_id="wrong", state="position_closed", pnl=1)), 1),
        ("t", json.dumps(dict(trade_id="t", state="position_closed", pnl=None)), 1),
    ],
)
async def test_closed_recovery_no_longer_silently_loses_corrupt_realized_pnl(bad):
    store, _ = make_store([bad])
    with pytest.raises(RuntimeError, match="unambiguous terminal"):
        await store.load_closed_trades()
    with pytest.raises(ValueError):
        await store.query_closed_history(limit=10)


async def test_valid_closed_recovery_preserves_all_records():
    r = row()
    store, _ = make_store([(r["trade_id"], json.dumps(r), 1)])
    trade = object()
    with patch("aitos.trading.persistent_state._trade_from_dict", return_value=trade):
        assert await store.load_closed_trades() == [trade]


def test_cumulative_curve_preserves_low_order_pnl_across_large_cancellation():
    report = analyze_closed_trades([row(0, 1e16), row(1, 1), row(2, -1e16)])
    assert report["curve"][-1]["net_pnl_usd"] == report["summary"]["net_pnl_usd"] == 1


def test_simultaneous_close_group_retains_correction_for_later_closes():
    rows = [row(0, 1e16), row(1, 1), row(2, -1e16)]
    rows[1]["exit_time"] = rows[0]["exit_time"]
    report = analyze_closed_trades(rows)
    assert report["curve"][-1]["net_pnl_usd"] == 1


@pytest.mark.parametrize("limit,offset", [(True, 0), (1.5, 0), (10, False), (10, 0.5)])
async def test_history_requires_integer_nonboolean_bounds(limit, offset):
    store, query = make_store([])
    with pytest.raises(ValueError):
        await store.query_closed_history(limit=limit, offset=offset)
    query.assert_not_awaited()


async def test_console_history_and_performance_have_throwing_engine_read_budgets():
    store, query = make_store([])
    await store.query_closed_history(limit=10)
    history_sql = query.call_args.args[0]
    await store.query_closed_performance()
    for sql in [history_sql, query.call_args.args[0]]:
        assert "max_execution_time=3" in sql
        assert "max_bytes_to_read=67108864" in sql
        assert "max_memory_usage=134217728" in sql
        assert "read_overflow_mode='throw'" in sql
