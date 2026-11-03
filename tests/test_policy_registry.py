from aitos.journal.policy_registry import PolicyRegistry


def test_policy_registry_persists_and_reloads(tmp_path):
    path = tmp_path / "active_policy.json"
    weights = {"order_flow_bias": 0.6, "auction_context": 0.4}
    first = PolicyRegistry(str(path), weights)
    active = first.activate("v2", weights, 0.65)
    assert active.version == "v2"

    second = PolicyRegistry(str(path), {"order_flow_bias": 1.0})
    assert second.active.version == "v2"
    assert second.active.weights == weights
    assert second.active.min_confidence == 0.65


def test_policy_registry_rejects_invalid_weights(tmp_path):
    registry = PolicyRegistry(str(tmp_path / "p.json"), {"a": 1.0})
    try:
        registry.activate("bad", {"a": 0.7})
    except ValueError as exc:
        assert "sum to 1" in str(exc)
    else:
        raise AssertionError("invalid weights were accepted")
