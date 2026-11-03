from aitos.journal.evidence_shadow import ShadowWeightResult
from aitos.journal.policy_governance import PolicyGovernance, PolicyVersion


def shadow(eligible=True):
    return ShadowWeightResult(
        1.0, 1.1, 0.1, 100, True, eligible, "eligible" if eligible else "rejected"
    )


def test_promotion_requires_explicit_approval():
    gov = PolicyGovernance(PolicyVersion("v1", {"amt": 1.0}, "now"))
    try:
        gov.propose_promotion("v2", {"amt": 1.0}, shadow(), approved=False)
        assert False
    except PermissionError:
        pass


def test_promotion_and_rollback():
    gov = PolicyGovernance(PolicyVersion("v1", {"amt": 1.0}, "now"))
    promoted = gov.propose_promotion(
        "v2", {"amt": 0.7, "order_flow": 0.3}, shadow(), approved=True
    )
    assert promoted.version == "v2"
    assert gov.active.version == "v2"
    assert gov.rollback().version == "v1"


def test_ineligible_candidate_is_rejected():
    gov = PolicyGovernance(PolicyVersion("v1", {"amt": 1.0}, "now"))
    try:
        gov.propose_promotion("v2", {"amt": 1.0}, shadow(False), approved=True)
        assert False
    except ValueError:
        pass
