from datetime import datetime, timezone, timedelta
from aitos.capital_intelligence.contracts import CapitalEvent
from aitos.capital_intelligence.intelligence import (
    assess_entity_behavior, candidate_links_from_shared_funding, evidence_from_candidate,
)

def _event(i, direction="in", amount=100.0):
    now = datetime.now(timezone.utc) + timedelta(seconds=i)
    return CapitalEvent(str(i), "test", "onchain", "crypto", "chain", "TOKEN",
        "token_transfer", now, now, amount, "token", amount, actor_id="wallet-a",
        counterparty_id="wallet-b", finality="finalized", confidence=.9,
        metadata={"flow_direction": direction})

def test_shared_counterparty_only_creates_candidate_not_verified():
    events = [_event(i) for i in range(3)]
    candidates = candidate_links_from_shared_funding(events)
    assert len(candidates) == 1
    evidence = evidence_from_candidate(candidates[0], observed_at=datetime.now(timezone.utc))
    assert evidence.status == "candidate"
    assert evidence.confidence < 1

def test_behavior_assessment_is_descriptive_and_bounded():
    profile = assess_entity_behavior("wallet-a", [_event(i) for i in range(4)])
    assert profile.accumulation_score == 1
    assert profile.distribution_score == 0
    assert profile.net_flow_usd == 400
    assert profile.hypotheses[0].label == "accumulation_candidate"

def test_reverted_events_are_excluded():
    events = [_event(1), _event(2)]
    reverted = CapitalEvent("r", "test", "onchain", "crypto", "chain", "TOKEN",
        "token_transfer", datetime.now(timezone.utc), datetime.now(timezone.utc),
        500, "token", 500, actor_id="wallet-a", finality="reverted")
    profile = assess_entity_behavior("wallet-a", events + [reverted])
    assert profile.event_count == 2

def test_evm_sender_is_treated_as_outflow_without_explicit_direction():
    now = datetime.now(timezone.utc)
    event = CapitalEvent(
        "evm-out", "evm:1", "onchain_transfer", "crypto_spot", "1", "TOKEN",
        "erc20_transfer", now, now, 1, "token", 250,
        actor_id="wallet-a", counterparty_id="wallet-b", finality="confirmed",
    )
    profile = assess_entity_behavior("wallet-a", [event])
    assert profile.net_flow_usd == -250
    assert profile.distribution_score == 1
