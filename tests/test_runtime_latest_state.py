"""Retained latest-state conflicts must block recovery, not disappear."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from aitos.trading.persistent_state import DurableTradingStateStore
from aitos.models.trade import TradeLifecycleState


def store(rows):
    query = AsyncMock(return_value=SimpleNamespace(result_rows=rows))
    return (
        DurableTradingStateStore(SimpleNamespace(_client=SimpleNamespace(query=query))),
        query,
    )


async def test_latest_state_is_selected_before_filtering_open_state():
    value, query = store([("t", json.dumps(dict(trade_id="t")), 1)])
    with patch(
        "aitos.trading.persistent_state._trade_from_dict",
        return_value=SimpleNamespace(state=TradeLifecycleState.POSITION_CLOSED),
    ):
        assert await value.load_open_trades() == []
    sql = query.call_args.args[0]
    assert "max(updated_at)" in sql
    assert "uniqExact(payload_json)" in sql
    assert "WHERE state IN" not in sql
    assert "paper_closed_trade_ledger" in sql


async def test_valid_latest_open_record_is_restored():
    value, _ = store([("t", json.dumps(dict(trade_id="t")), 1)])
    trade = SimpleNamespace(state=TradeLifecycleState.POSITION_OPENED)
    with patch("aitos.trading.persistent_state._trade_from_dict", return_value=trade):
        assert await value.load_open_trades() == [trade]


@pytest.mark.parametrize(
    "row",
    [
        ("t", "{}", 2),
        ("t", "{}", 0),
        ("t", "invalid json", 1),
        ("t", json.dumps(dict(trade_id="wrong")), 1),
        ("t", "{}"),
    ],
)
async def test_conflicting_corrupt_or_unversioned_latest_record_blocks(row):
    value, _ = store([row])
    with pytest.raises(RuntimeError, match="unambiguous valid latest"):
        await value.load_open_trades()
