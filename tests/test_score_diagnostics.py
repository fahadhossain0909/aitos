from aitos.intelligence.score_diagnostics import build_score_breakdown


def test_score_breakdown_matches_existing_normalization():
    scores = {"a": 5.0, "b": 8.0}
    weights = {"a": 0.25, "b": 0.75}

    contributions, weight_total, normalized = build_score_breakdown(scores, weights)

    assert weight_total == 1.0
    assert normalized == 72.5
    assert contributions[0].weighted_contribution == 1.25
    assert contributions[1].weighted_contribution == 6.0


def test_score_breakdown_keeps_missing_weight_at_zero():
    contributions, weight_total, normalized = build_score_breakdown(
        {"a": 5.0, "unweighted": 9.0}, {"a": 1.0}
    )

    assert weight_total == 1.0
    assert normalized == 50.0
    assert contributions[1].weight == 0.0
