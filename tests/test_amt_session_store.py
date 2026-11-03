from datetime import datetime, timedelta, timezone

from aitos.intelligence.amt import SessionProfileStore, SessionSnapshot


def test_session_store_keeps_previous_and_recent():
    store = SessionProfileStore(max_sessions=3)
    # Use the actual engine elsewhere; this test only verifies store semantics.
    from aitos.intelligence.amt import AMTEngine
    from aitos.models.market import TradeSide, TradeTick

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    engine = AMTEngine(tick_size=1)
    contexts = []
    for i in range(2):
        trades = [
            TradeTick(
                symbol="BTCUSDT",
                trade_id=i * 10 + j,
                price=100 + i + j * 0.1,
                quantity=1.0,
                side=TradeSide.BUY,
                is_buyer_maker=False,
                timestamp=base + timedelta(days=i, minutes=j),
            )
            for j in range(10)
        ]
        contexts.append(engine.analyze(trades, session_start=base + timedelta(days=i)))
    store.upsert(SessionSnapshot("s1", base, base + timedelta(days=1), contexts[0]))
    store.upsert(
        SessionSnapshot(
            "s2", base + timedelta(days=1), base + timedelta(days=2), contexts[1]
        )
    )
    assert store.previous("s2").session_id == "s1"
    assert store.recent(1)[0].session_id == "s2"
