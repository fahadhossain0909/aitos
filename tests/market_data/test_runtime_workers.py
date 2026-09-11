from aitos.market_data.runtime import GATEWAY_DRAIN_WORKERS


def test_canonical_gateway_uses_four_drain_workers():
    assert GATEWAY_DRAIN_WORKERS == 4
