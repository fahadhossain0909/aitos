from math import nan

from aitos.intelligence.capital_feedback import (
    CapitalFeedback,
    CapitalFeedbackConfig,
    CapitalOutcome,
)


def _outcome(
    index: int,
    *,
    loss: bool = False,
    probability: float = 0.2,
) -> CapitalOutcome:
    return CapitalOutcome(
        symbol=f"ASSET{index}",
        realized_return_pct=-0.5 if loss else 0.8,
        predicted_loss_probability=probability,
        predicted_net_edge_pct=0.5,
        realized_cost_pct=0.12,
        regime="risk_on",
        model_id="baseline",
    )


def test_feedback_window_is_bounded_and_snapshot_is_stable() -> None:
    feedback = CapitalFeedback(CapitalFeedbackConfig(window_size=2, min_samples=2))
    feedback.extend((_outcome(1), _outcome(2), _outcome(3, loss=True)))

    snapshot = feedback.snapshot()
    assert snapshot.sample_count == 2
    assert snapshot.realized_loss_rate == 0.5
    assert snapshot.mean_cost_pct == 0.12
    assert snapshot.by_regime == {"risk_on": 2}
    assert snapshot.by_model == {"baseline": 2}


def test_feedback_calibration_is_observational() -> None:
    feedback = CapitalFeedback(CapitalFeedbackConfig(min_samples=3))
    feedback.extend(
        (
            _outcome(1, loss=True, probability=0.2),
            _outcome(2, loss=True, probability=0.2),
            _outcome(3, probability=0.2),
        )
    )

    assert feedback.ready()
    calibration = feedback.probability_calibration()
    bucket = calibration[2]
    assert bucket == (3, 2, 2 / 3)
    assert feedback.snapshot().brier_score > 0.0


def test_feedback_rejects_non_finite_probability() -> None:
    feedback = CapitalFeedback()
    try:
        feedback.record(_outcome(1, probability=nan))
    except ValueError as exc:
        assert "predicted_loss_probability" in str(exc)
    else:
        raise AssertionError("non-finite probability must be rejected")
