from aitos.intelligence.graph_context import _directional_score


def test_graph_context_score_is_neutral_without_cases():
    assert _directional_score([], "long") == 5.0


def test_graph_context_score_rewards_resolved_positive_cases_for_long():
    rows = [
        {"outcome": "win", "pnl": 10.0, "score": 5},
        {"outcome": "loss", "pnl": -2.0, "score": 1},
    ]
    assert _directional_score(rows, "long") > 5.0


def test_graph_context_score_inverts_for_short():
    rows = [{"outcome": "win", "pnl": 10.0, "score": 5}]
    assert _directional_score(rows, "short") < 5.0
