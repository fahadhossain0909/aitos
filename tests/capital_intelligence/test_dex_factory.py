import pytest

from aitos.capital_intelligence.dex_factory import (
    EvmDexFactoryDiscoveryAdapter,
    V2_PAIR_CREATED_TOPIC,
    V3_POOL_CREATED_TOPIC,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol,topics",
    [
        (
            "uniswap_v2",
            lambda token0, token1: [
                V2_PAIR_CREATED_TOPIC,
                "0x" + "0" * 24 + token0[2:],
                "0x" + "0" * 24 + token1[2:],
            ],
        ),
        (
            "uniswap_v3",
            lambda token0, token1: [
                V3_POOL_CREATED_TOPIC,
                "0x" + "0" * 24 + token0[2:],
                "0x" + "0" * 24 + token1[2:],
                "0x" + f"{3000:064x}",
            ],
        ),
    ],
)
async def test_factory_discovery_decodes_pool_and_token_addresses(protocol, topics):
    factory = "0x" + "1" * 40
    token0 = "0x" + "2" * 40
    token1 = "0x" + "3" * 40
    pool = "0x" + "4" * 40
    adapter = EvmDexFactoryDiscoveryAdapter(
        rpc_url="https://rpc.example",
        chain_id="1",
        factories={factory: protocol},
        confirmations=2,
        max_block_range=10,
    )
    words = (
        ["0" * 24 + pool[2:], f"{2:064x}"]
        if protocol.endswith("_v2")
        else [f"{60:064x}", "0" * 24 + pool[2:]]
    )
    log = {
        "address": factory,
        "topics": topics(token0, token1),
        "data": "0x" + "".join(words),
        "blockNumber": "0x20",
        "blockHash": "0x" + "a" * 64,
        "transactionHash": "0x" + "b" * 64,
        "logIndex": "0x1",
    }

    async def fake_rpc(method, params):
        if method == "eth_blockNumber":
            return "0x22"
        if method == "eth_getLogs":
            assert params[0]["topics"][0] == [
                V2_PAIR_CREATED_TOPIC,
                V3_POOL_CREATED_TOPIC,
            ]
            return [log]
        if method == "eth_getBlockByNumber":
            return {
                "timestamp": hex(1_700_000_000),
                "hash": "0x" + "a" * 64,
            }
        raise AssertionError(method)

    adapter._rpc = fake_rpc
    events, cursor = await adapter.poll(cursor="0x1f")
    assert cursor == "0x20"
    assert len(events) == 1
    event = events[0]
    assert event.instrument == pool
    assert event.metadata["token0_address"] == token0
    assert event.metadata["token1_address"] == token1
    assert event.metadata["pool_not_auto_activated"] is True
    assert event.finality == "confirmed"
    if protocol.endswith("_v3"):
        assert event.metadata["fee_tier"] == 3000
        assert event.metadata["tick_spacing"] == 60


def test_factory_discovery_requires_allowlisted_factories_and_supported_protocols():
    with pytest.raises(ValueError):
        EvmDexFactoryDiscoveryAdapter(
            rpc_url="https://rpc.example",
            chain_id="1",
            factories={"0x" + "1" * 40: "unknown_protocol"},
        )
    with pytest.raises(ValueError):
        EvmDexFactoryDiscoveryAdapter(
            rpc_url="http://rpc.example",
            chain_id="1",
            factories={"0x" + "1" * 40: "uniswap_v2"},
        )
