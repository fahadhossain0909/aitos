from datetime import datetime, timedelta, timezone

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.recovery import recoverable_events


def _event(source, age=0):
    return MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        market="usd_m_futures",
        symbol="BTCUSDT",
        event_time=datetime.now(timezone.utc) - timedelta(seconds=age),
        payload={"price": 100},
        source=source,
    )


def test_recent_rest_recovery_is_kept_as_rest():
    event = _event(MarketSource.REST, age=5)
    result = recoverable_events([event])
    assert result == [event]
    assert result[0].source is MarketSource.REST


def test_old_rest_recovery_is_discarded():
    assert recoverable_events([_event(MarketSource.REST, age=61)]) == []


def test_websocket_event_is_not_changed():
    event = _event(MarketSource.WEBSOCKET, age=120)
    assert recoverable_events([event]) == [event]
