from datetime import datetime, timedelta, timezone
from aitos.intelligence.historical_validation import (
    evaluate_historical_analogues_walk_forward,
)
from aitos.models.market import Kline


def test_walk_forward_returns_explicit_insufficient_evidence_for_short_history():
    rows = []
    for i in range(30):
        t = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
        rows.append(
            Kline(
                "BTCUSDT",
                "1m",
                t,
                t + timedelta(minutes=1),
                100,
                101,
                99,
                100,
                10,
                1000,
                5,
                5,
                500,
            )
        )
    report = evaluate_historical_analogues_walk_forward(
        rows, window=10, horizon=3, min_analogue_count=2
    )
    assert report["available"] is False
    assert report["sample_count"] == 0


def test_walk_forward_is_explicitly_oos_and_does_not_auto_promote():
    rows = []
    for i in range(220):
        t = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
        price = 100 + i * 0.03 + ((i % 17) - 8) * 0.2
        rows.append(
            Kline(
                "BTCUSDT",
                "1m",
                t,
                t + timedelta(minutes=1),
                price,
                price + 0.4,
                price - 0.4,
                price,
                100,
                10000,
                10,
                50,
                5000,
            )
        )
    report = evaluate_historical_analogues_walk_forward(
        rows,
        window=10,
        horizon=3,
        search_back=120,
        top_k=6,
        min_analogue_count=2,
        step=3,
    )
    assert report["leakage_policy"].startswith("prefix-only")
    assert (
        report["calibration_status"]
        == "evaluation_report_only_not_automatically_promoted"
    )
    if report["available"]:
        assert report["sample_count"] > 0
        assert 0 <= report["brier_score"] <= 2
