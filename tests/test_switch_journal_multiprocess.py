from __future__ import annotations

import sqlite3
from concurrent.futures import ProcessPoolExecutor

from aitos.trading.position_switching import SQLiteSwitchJournal, SwitchIntent


def _create_competing_intent(args: tuple[str, str]) -> str:
    path, switch_id = args
    journal = SQLiteSwitchJournal(path)
    intent = SwitchIntent(
        switch_id=switch_id,
        incumbent_trade_id="same-incumbent",
        incumbent_symbol="BTCUSDT",
        candidate_opportunity_id=f"candidate-{switch_id}",
        candidate_symbol="ETHUSDT",
        state="close_pending",
        close_client_order_id=f"close-{switch_id}",
    )
    try:
        journal.create(intent)
        return "created"
    except sqlite3.IntegrityError:
        return "conflict"


def test_sqlite_journal_serializes_competing_processes_for_one_incumbent(tmp_path):
    path = str(tmp_path / "multi-process-switch.sqlite")
    # Each worker creates its own journal/SQLite connection in a separate
    # process. The database-level partial unique index, not an asyncio lock,
    # must arbitrate the race.
    with ProcessPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(
                _create_competing_intent,
                [(path, f"switch-{index}") for index in range(8)],
            )
        )

    assert outcomes.count("created") == 1
    assert outcomes.count("conflict") == 7
    journal = SQLiteSwitchJournal(path)
    incomplete = journal.list_incomplete()
    assert len(incomplete) == 1
    assert incomplete[0].incumbent_trade_id == "same-incumbent"


def test_sqlite_journal_allows_next_switch_after_terminal_completion(tmp_path):
    journal = SQLiteSwitchJournal(tmp_path / "terminal-switch.sqlite")
    first = SwitchIntent(
        switch_id="switch-first",
        incumbent_trade_id="trade-1",
        incumbent_symbol="BTCUSDT",
        candidate_opportunity_id="op-1",
        candidate_symbol="ETHUSDT",
        state="close_pending",
        close_client_order_id="close-first",
    )
    journal.create(first)
    journal.update("switch-first", state="completed")

    second = SwitchIntent(
        switch_id="switch-second",
        incumbent_trade_id="trade-1",
        incumbent_symbol="BTCUSDT",
        candidate_opportunity_id="op-2",
        candidate_symbol="SOLUSDT",
        state="close_pending",
        close_client_order_id="close-second",
    )
    journal.create(second)

    assert [item.switch_id for item in journal.list_incomplete()] == ["switch-second"]
