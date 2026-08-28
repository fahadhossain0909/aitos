from pathlib import Path

from scripts.redis_stream_archive import maxlen_for


def test_known_stream_families_are_bounded() -> None:
    assert maxlen_for("stream:market.trade.BTCUSDT") == 25_000
    assert maxlen_for("stream:market.orderbook.BTCUSDT") == 25_000
    assert maxlen_for("stream:market.liquidity.BTCUSDT") == 100_000
    assert maxlen_for("stream:market.orderflow.BTCUSDT") == 25_000
    assert maxlen_for("stream:decision.generated") == 10_000
    assert maxlen_for("stream:journal.decision_recorded") == 10_000
    assert maxlen_for("stream:trade.position_opened") == 10_000
    assert maxlen_for("stream:risk.score_update") == 10_000


def test_unknown_streams_have_a_safe_default_bound() -> None:
    assert maxlen_for("stream:future.new_topic") == 5_000
