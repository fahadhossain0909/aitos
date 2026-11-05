"""Unit tests for conservative Binance income-to-trade reconciliation."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aitos.exchange.cost_adjustments import BinanceIncomeReconciliationService


class FakeEventBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


class FakeExchange:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []

    async def fetch_income_history(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.rows)


class FakeStateStore:
    def __init__(self, cursor=None):
        self.cursor = cursor
        self.saved = []

    async def load_income_cursor(self, account):
        return self.cursor

    async def save_income_cursor(self, cursor_time_ms, account):
        self.cursor = cursor_time_ms
        self.saved.append((account, cursor_time_ms))


class FakeLifecycle:
    def __init__(self, open_trades=None, closed_trades=None):
        self.open_trades = list(open_trades or [])
        self.closed_trades = list(closed_trades or [])

    def get_open_trades(self):
        return list(self.open_trades)

    def get_closed_trades(self):
        return list(self.closed_trades)


def make_trade(
    trade_id,
    *,
    symbol="BTCUSDT",
    entry_time=None,
    exit_time=None,
    state="position_opened",
):
    entry_time = entry_time or datetime.now(timezone.utc) - timedelta(hours=1)
    return SimpleNamespace(
        trade_id=trade_id,
        symbol=symbol,
        entry_time=entry_time.isoformat(),
        exit_time=exit_time.isoformat() if exit_time else None,
        state=SimpleNamespace(value=state),
    )


def make_service(exchange, lifecycle, bus=None, store=None):
    bus = bus or FakeEventBus()
    store = store or FakeStateStore()
    service = BinanceIncomeReconciliationService(
        exchange, lifecycle, bus, store
    )
    return service, bus, store


def test_resolver_matches_only_one_position_for_symbol_and_exchange_time():
    entry = datetime.now(timezone.utc) - timedelta(hours=1)
    lifecycle = FakeLifecycle([make_trade("position-1", entry_time=entry)])
    service, _, _ = make_service(FakeExchange(), lifecycle)

    row = {
        "incomeType": "FUNDING_FEE",
        "symbol": "BTCUSDT",
        "time": int((entry + timedelta(minutes=30)).timestamp() * 1000),
    }

    assert service.resolve_trade_id(row) == "position-1"


def test_resolver_refuses_ambiguous_overlapping_positions():
    entry = datetime.now(timezone.utc) - timedelta(hours=1)
    lifecycle = FakeLifecycle(
        [
            make_trade("position-1", entry_time=entry),
            make_trade("position-2", entry_time=entry + timedelta(minutes=1)),
        ]
    )
    service, _, _ = make_service(FakeExchange(), lifecycle)
    row = {
        "incomeType": "FUNDING_FEE",
        "symbol": "BTCUSDT",
        "time": int((entry + timedelta(minutes=30)).timestamp() * 1000),
    }

    assert service.resolve_trade_id(row) is None


def test_commission_allows_small_post_close_exchange_timestamp_lag():
    entry = datetime.now(timezone.utc) - timedelta(hours=1)
    exit_time = entry + timedelta(minutes=30)
    closed = make_trade(
        "closed-1",
        entry_time=entry,
        exit_time=exit_time,
        state="position_closed",
    )
    service, _, _ = make_service(FakeExchange(), FakeLifecycle(closed_trades=[closed]))
    row_time = exit_time + timedelta(seconds=30)

    assert service.resolve_trade_id(
        {
            "incomeType": "COMMISSION",
            "symbol": "BTCUSDT",
            "time": int(row_time.timestamp() * 1000),
        }
    ) == "closed-1"
    assert service.resolve_trade_id(
        {
            "incomeType": "FUNDING_FEE",
            "symbol": "BTCUSDT",
            "time": int(row_time.timestamp() * 1000),
        }
    ) is None


@pytest.mark.asyncio
async def test_run_once_publishes_fee_and_advances_durable_cursor():
    entry = datetime.now(timezone.utc) - timedelta(hours=1)
    lifecycle = FakeLifecycle([make_trade("position-1", entry_time=entry)])
    now = datetime.now(timezone.utc)
    exchange = FakeExchange(
        [
            {
                "incomeType": "COMMISSION",
                "income": "-0.12",
                "tranId": 9001,
                "asset": "USDT",
                "symbol": "BTCUSDT",
                "time": int((now - timedelta(seconds=10)).timestamp() * 1000),
            }
        ]
    )
    service, bus, store = make_service(exchange, lifecycle)

    result = await service.run_once()

    assert result == {"rows": 1, "published": 1, "unattributed": 0}
    assert bus.events[0].payload["trade_id"] == "position-1"
    assert bus.events[0].payload["adjustment_id"] == "binance:income:9001"
    assert bus.events[0].payload["kind"] == "fee"
    assert bus.events[0].payload["amount_usd"] == 0.12
    assert store.saved and store.cursor >= int(now.timestamp() * 1000)
    assert exchange.calls[0]["start_time_ms"] <= int(entry.timestamp() * 1000)


@pytest.mark.asyncio
async def test_unattributed_income_holds_cursor_before_ambiguous_row():
    entry = datetime.now(timezone.utc) - timedelta(hours=1)
    lifecycle = FakeLifecycle(
        [
            make_trade("position-1", entry_time=entry),
            make_trade("position-2", entry_time=entry + timedelta(minutes=1)),
        ]
    )
    row_time = int(datetime.now(timezone.utc).timestamp() * 1000) - 100
    exchange = FakeExchange(
        [
            {
                "incomeType": "FUNDING_FEE",
                "income": "-0.03",
                "tranId": 9002,
                "asset": "USDT",
                "symbol": "BTCUSDT",
                "time": row_time,
            }
        ]
    )
    service, bus, store = make_service(exchange, lifecycle)

    result = await service.run_once()

    assert result["unattributed"] == 1
    assert bus.events == []
    assert store.cursor == row_time - 1


@pytest.mark.asyncio
async def test_no_trade_history_skips_unattributable_account_backfill():
    exchange = FakeExchange()
    service, bus, store = make_service(exchange, FakeLifecycle())

    result = await service.run_once()

    assert result == {"rows": 0, "published": 0, "unattributed": 0}
    assert exchange.calls == []
    assert bus.events == []
    assert store.cursor is not None


@pytest.mark.asyncio
async def test_exchange_failure_does_not_advance_cursor():
    class FailingExchange:
        async def fetch_income_history(self, **kwargs):
            raise RuntimeError("exchange unavailable")

    entry = datetime.now(timezone.utc) - timedelta(hours=1)
    service, _, store = make_service(
        FailingExchange(), FakeLifecycle([make_trade("position-1", entry_time=entry)])
    )

    with pytest.raises(RuntimeError, match="exchange unavailable"):
        await service.run_once()

    assert store.saved == []
    assert store.cursor is None

@pytest.mark.asyncio
async def test_durable_store_persists_exchange_income_cursor():
    from types import SimpleNamespace

    from aitos.trading.persistent_state import DurableTradingStateStore

    class FakeClickHouse:
        def __init__(self):
            self.commands = []
            self.inserted = []
            self.query_rows = [(123456,)]

        async def command(self, query):
            self.commands.append(query)

        async def query(self, query, parameters=None):
            self.last_query = (query, parameters)
            return SimpleNamespace(result_rows=self.query_rows)

        async def insert(self, table, rows, column_names=None):
            self.inserted.append((table, rows, column_names))

    client = FakeClickHouse()
    store = DurableTradingStateStore(SimpleNamespace(_client=client))

    await store.initialize()
    assert any("exchange_income_sync_cursor" in query for query in client.commands)
    assert await store.load_income_cursor("binance_usdm") == 123456

    await store.save_income_cursor(123999, "binance_usdm")
    table, rows, columns = client.inserted[-1]
    assert table == "exchange_income_sync_cursor"
    assert rows[0][0:2] == ["binance_usdm", 123999]
    assert columns == ["account", "cursor_time_ms", "updated_at"]
