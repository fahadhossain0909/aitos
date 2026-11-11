from datetime import datetime, timedelta, timezone
from aitos.intelligence.historical_analogue import search_historical_analogues
from aitos.models.market import Kline


def test_analogue_outcome_is_measured_after_matched_pattern():
    prices = (
        [100 + i for i in range(12)]
        + [100 + i for i in range(12)]
        + [300 + i for i in range(12)]
    )
    rows = []
    for i, p in enumerate(prices):
        t = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
        rows.append(
            Kline(
                "BTCUSDT",
                "1m",
                t,
                t + timedelta(minutes=1),
                p,
                p + 1,
                p - 1,
                p,
                100,
                10000,
                10,
                50,
                5000,
            )
        )
    matches = search_historical_analogues(
        rows, window=6, search_back=30, top_k=5, forward_horizon=2
    )
    assert matches
    # Outcome is anchored at the last candle of each analogue window and advances two bars.
    for match in matches:
        base = rows[match.start_index + 5].close
        end = rows[match.start_index + 7].close
        if match.outcome:
            assert (
                match.outcome.median_return == (end - base) / base
                or match.outcome.best_return >= (end - base) / base
            )
