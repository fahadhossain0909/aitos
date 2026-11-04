from aitos.strategy.allocator import CapitalAllocator
from aitos.strategy.contracts import CapitalRequest


def request(strategy_id, notional, loss, edge, confidence=0.8, priority=1.0):
    return CapitalRequest(
        strategy_id=strategy_id,
        requested_notional=notional,
        max_loss=loss,
        expected_edge=edge,
        confidence=confidence,
        priority=priority,
    )


def test_batch_allocator_is_order_independent_and_respects_capital_budget():
    allocator = CapitalAllocator(10_000, max_strategy_fraction=0.8)
    requests = [
        request("alpha", 8_000, 80, 8),
        request("beta", 8_000, 80, 4),
        request("gamma", 8_000, 80, 2),
    ]
    first = allocator.allocate(requests, available_capital=6_000)
    second = allocator.allocate(list(reversed(requests)), available_capital=6_000)

    assert first == second
    assert sum(item.notional for item in first) <= 6_000
    assert {item.strategy_id for item in first} == {"alpha", "beta", "gamma"}
    assert first[0].notional > first[1].notional > first[2].notional


def test_batch_allocator_enforces_aggregate_projected_loss_budget():
    allocator = CapitalAllocator(10_000, max_strategy_fraction=1.0)
    allocations = allocator.allocate(
        [
            request("alpha", 5_000, 500, 8),
            request("beta", 5_000, 500, 6),
        ],
        risk_budget=100,
    )

    assert allocations
    assert sum(item.projected_loss for item in allocations) <= 100.000001


def test_batch_allocator_redistributes_unused_capacity_after_request_caps():
    allocator = CapitalAllocator(10_000, max_strategy_fraction=1.0)
    allocations = allocator.allocate(
        [
            request("small", 100, 1, 10),
            request("large", 10_000, 100, 5),
        ],
        available_capital=5_000,
    )
    by_id = {item.strategy_id: item.notional for item in allocations}

    assert by_id["small"] == 100
    assert by_id["large"] == 4_900


def test_batch_allocator_rejects_invalid_and_duplicate_strategy_requests():
    allocator = CapitalAllocator(1_000)
    allocations = allocator.allocate(
        [
            request("valid", 100, 2, 1),
            request("valid", 200, 4, 2),
            request("negative-risk", 100, -1, 1),
            request("nan-edge", 100, 1, float("nan")),
        ]
    )
    assert [item.strategy_id for item in allocations] == ["valid"]
