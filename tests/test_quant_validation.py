from datetime import datetime, timedelta, timezone

import pytest

from aitos.evaluation.quant_validation import (
    chronological_walk_forward_splits,
    comparative_report,
    evaluate_fold_results,
    validate_observations,
)


def observations(count=20):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(count):
        decision = start + timedelta(days=i)
        rows.append(
            {
                "decision_at": decision.isoformat(),
                "outcome_at": (decision + timedelta(hours=1)).isoformat(),
                "side": "LONG" if i % 2 == 0 else "SHORT",
                "net_pnl": 1.0 if i % 3 else -0.5,
                "variant": "baseline" if i % 2 == 0 else "no_orderflow",
            }
        )
    return rows


def test_walk_forward_is_chronological_and_embargoes_overlapping_outcomes():
    rows = observations()
    folds = chronological_walk_forward_splits(
        rows,
        train_fraction=0.5,
        test_fraction=0.2,
        step_fraction=0.2,
        embargo=timedelta(hours=2),
    )
    assert folds
    for fold in folds:
        assert all(
            rows[i]["outcome_at"] <= fold.test_start.isoformat()
            for i in fold.train_indices
        )
        assert fold.train_end == max(
            datetime.fromisoformat(rows[i]["decision_at"])
            for i in fold.train_indices
        )


def test_validation_rejects_lookahead_and_naive_timestamps():
    rows = observations(2)
    rows[0]["outcome_at"] = rows[0]["decision_at"]
    with pytest.raises(ValueError):
        validate_observations(rows)
    rows = observations(2)
    rows[0]["decision_at"] = "2026-01-01T00:00:00"
    with pytest.raises(ValueError):
        validate_observations(rows)


def test_comparative_report_separates_long_short_and_ablation_variants():
    report = comparative_report(observations())
    assert set(report) == {"baseline", "no_orderflow"}
    assert "LONG" in report["baseline"]["by_side"]
    assert "SHORT" in report["no_orderflow"]["by_side"]


def test_fold_results_score_only_held_out_observations():
    rows = observations()
    folds = chronological_walk_forward_splits(
        rows, train_fraction=0.5, test_fraction=0.2
    )
    results = evaluate_fold_results(rows, folds)
    assert results
    assert all(result["test_trade_count"] > 0 for result in results)
