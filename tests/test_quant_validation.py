from datetime import datetime, timedelta, timezone

import pytest

from aitos.evaluation.quant_validation import (
    chronological_walk_forward_splits,
    comparative_report,
    evaluate_fold_results,
    expectancy_confidence_interval,
    probability_calibration,
    rejected_opportunity_counterfactual,
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
            datetime.fromisoformat(rows[i]["decision_at"]) for i in fold.train_indices
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


def test_quant_scorecard_reports_mae_mfe_costs_and_regime_splits():
    rows = observations(8)
    for index, row in enumerate(rows):
        row.update(
            regime="trending" if index % 2 == 0 else "ranging",
            mae_pct=0.2 + index / 100,
            mfe_pct=0.5 + index / 100,
            entry_capture_pct=0.4,
            slippage_bps=1.5,
            fees_usd=0.02,
            funding_usd=-0.01,
        )
    report = comparative_report(rows)
    baseline = report["baseline"]
    assert set(baseline["by_regime"]) == {"trending"}
    assert baseline["research_diagnostics"]["mean_mae_pct"] is not None
    assert baseline["research_diagnostics"]["mean_mfe_pct"] is not None
    assert baseline["research_diagnostics"]["mean_slippage_bps"] == 1.5
    assert baseline["expectancy_confidence_interval"]["samples"] == 4


def test_rejected_opportunity_counterfactual_reports_missed_markouts():
    rows = observations(4)
    for index, row in enumerate(rows):
        row["rejected_opportunity"] = True
        row["counterfactual_net_pnl"] = 2.0 if index % 2 == 0 else -1.0
    result = rejected_opportunity_counterfactual(validate_observations(rows))
    assert result["samples"] == 4
    assert result["positive_markout_rate"] == 0.5
    assert result["negative_markout_rate"] == 0.5


def test_expectancy_interval_is_not_claimed_with_insufficient_samples():
    assert expectancy_confidence_interval([{"net_pnl": 1.0}])["lower"] is None
    assert expectancy_confidence_interval([])["upper"] is None


def test_probability_calibration_reports_brier_log_loss_and_reliability():
    rows = observations(4)
    probabilities = [0.9, 0.8, 0.2, 0.1]
    for index, row in enumerate(rows):
        row["predicted_win_probability"] = probabilities[index]
    result = probability_calibration(validate_observations(rows), bins=5)
    assert result["samples"] == 4
    assert 0.0 <= result["brier_score"] <= 1.0
    assert result["log_loss"] > 0.0
    assert result["reliability"]


def test_probability_calibration_rejects_invalid_forecast_probability():
    rows = observations(2)
    rows[0]["predicted_win_probability"] = 1.1
    with pytest.raises(ValueError, match="predicted_win_probability"):
        validate_observations(rows)
