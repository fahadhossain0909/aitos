from aitos.journal.evidence_attribution import EvidencePerformance
from aitos.journal.evidence_weight_optimizer import EvidenceWeightOptimizer


def perf(source, observations, alignment, avg_r):
    return EvidencePerformance(
        source,
        observations,
        int(observations * alignment),
        10,
        5,
        10.0,
        avg_r,
        alignment,
        0.66,
    )


def test_optimizer_is_bounded_and_preserves_total_weight():
    base = {"amt": 0.10, "order_flow": 0.15, "liquidity": 0.10, "rl": 0.10}
    candidate = EvidenceWeightOptimizer(
        min_observations=20, max_relative_change=0.20
    ).propose(
        base, [perf("order_flow", 100, 0.8, 0.3), perf("amt", 100, 0.4, -0.2)], "c1"
    )
    assert abs(sum(candidate.weights.values()) - sum(base.values())) < 1e-9
    for key, value in candidate.weights.items():
        assert base[key] * 0.8 - 1e-9 <= value <= base[key] * 1.2 + 1e-9


def test_optimizer_does_not_move_unsampled_component():
    base = {"amt": 0.10, "order_flow": 0.15}
    candidate = EvidenceWeightOptimizer(min_observations=50).propose(
        base, [perf("order_flow", 10, 1.0, 1.0)], "c2"
    )
    assert candidate.sample_counts["amt"] == 0
    assert candidate.confidence["amt"] == 0.0
