from datetime import datetime, timezone
import json
import pytest

from aitos.capital_intelligence.evm import EVMLogCapitalAdapter, TRANSFER_TOPIC
from aitos.capital_intelligence.dex import EvmDexSwapAdapter, V3_SWAP_TOPIC
from aitos.capital_intelligence.solana import SolanaAddressActivityAdapter


@pytest.mark.asyncio
async def test_evm_uses_block_time_and_preserves_raw_transfer_amount():
    adapter = EVMLogCapitalAdapter(
        rpc_url="https://rpc.example",
        chain_id="1",
        contracts=["0x" + "1" * 40],
        confirmations=2,
        max_block_range=10,
    )
    raw_amount = 2**200
    log = {
        "topics": [
            TRANSFER_TOPIC,
            "0x" + "0" * 24 + "2" * 40,
            "0x" + "0" * 24 + "3" * 40,
        ],
        "data": hex(raw_amount),
        "blockNumber": "0x19",
        "transactionHash": "0x" + "a" * 64,
        "logIndex": "0x0",
        "blockHash": "0x" + f"{25:064x}",
        "address": "0x" + "1" * 40,
    }

    async def fake_rpc(method, params):
        if method == "eth_blockNumber":
            return "0x20"
        if method == "eth_getLogs":
            return [log]
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            return {
                "timestamp": hex(1_700_000_000),
                "hash": "0x" + f"{number:064x}",
            }
        raise AssertionError(method)

    adapter._rpc = fake_rpc
    events, cursor = await adapter.poll(cursor="0x14")
    cursor_state = json.loads(cursor)
    assert cursor_state["head_block"] == 0x1e
    assert len(cursor_state["recent_hashes"]) == 0x1e + 1
    assert len(events) == 1
    assert events[0].event_time == datetime.fromtimestamp(1_700_000_000, timezone.utc)
    assert events[0].metadata["raw_amount"] == str(raw_amount)


@pytest.mark.asyncio
async def test_solana_cursor_is_independent_per_address_and_token_delta_is_signed():
    address_a = "wallet-A"
    address_b = "wallet-B"
    adapter = SolanaAddressActivityAdapter(
        rpc_url="https://solana.example",
        addresses=[address_a, address_b],
    )

    async def fake_rpc(method, params):
        if method == "getSignaturesForAddress":
            address, options = params
            old = "old-A" if address == address_a else "old-B"
            new = "new-A" if address == address_a else "new-B"
            return [{"signature": new}, {"signature": old}]
        if method == "getTransaction":
            signature = params[0]
            owner = address_a if signature.endswith("A") else address_b
            return {
                "slot": 10,
                "blockTime": 1_700_000_000,
                "transaction": {"message": {"accountKeys": [{"pubkey": owner}]}},
                "meta": {
                    "err": None,
                    "preBalances": [100],
                    "postBalances": [110],
                    "preTokenBalances": [
                        {
                            "owner": owner,
                            "mint": "USDC",
                            "accountIndex": 0,
                            "uiTokenAmount": {"amount": "500", "decimals": 6},
                        },
                        {
                            "owner": owner,
                            "mint": "USDC",
                            "accountIndex": 1,
                            "uiTokenAmount": {"amount": "200", "decimals": 6},
                        },
                    ],
                    "postTokenBalances": [
                        {
                            "owner": owner,
                            "mint": "USDC",
                            "accountIndex": 0,
                            "uiTokenAmount": {"amount": "700", "decimals": 6},
                        },
                        {
                            "owner": owner,
                            "mint": "USDC",
                            "accountIndex": 1,
                            "uiTokenAmount": {"amount": "250", "decimals": 6},
                        },
                    ],
                },
            }
        raise AssertionError(method)

    adapter._rpc = fake_rpc
    events, cursor = await adapter.poll(
        cursor='{"wallet-A":"old-A","wallet-B":"old-B"}'
    )
    import json

    assert json.loads(cursor) == {"wallet-A": "new-A", "wallet-B": "new-B"}
    token_events = [event for event in events if event.instrument == "USDC"]
    assert len(token_events) == 2
    assert all(event.metadata["flow_sign"] == 1 for event in token_events)
    assert all(event.metadata["raw_delta"] == "250" for event in token_events)



@pytest.mark.asyncio
async def test_dex_swap_resolves_pool_tokens_and_keeps_signed_raw_deltas():
    pool = "0x" + "1" * 40
    token0 = "0x" + "2" * 40
    token1 = "0x" + "3" * 40
    actor = "0x" + "4" * 40
    adapter = EvmDexSwapAdapter(
        rpc_url="https://rpc.example",
        chain_id="1",
        pools={pool: "uniswap_v3"},
        confirmations=2,
        max_block_range=10,
    )
    negative = (1 << 256) - 1_000_000
    words = [negative, 2_000_000, 0, 0, 0]
    log = {
        "topics": [V3_SWAP_TOPIC, "0x" + "0" * 24 + actor[2:]],
        "data": "0x" + "".join(f"{value:064x}" for value in words),
        "blockNumber": "0x20",
        "transactionHash": "0x" + "a" * 64,
        "logIndex": "0x0",
        "blockHash": "0x" + "b" * 64,
        "address": pool,
    }

    async def fake_rpc(method, params):
        if method == "eth_blockNumber":
            return "0x22"
        if method == "eth_getLogs":
            return [log]
        if method == "eth_call":
            return "0x" + "0" * 24 + (token0 if params[0]["data"] == "0x0dfe1681" else token1)[2:]
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(1_700_000_000)}
        raise AssertionError(method)

    adapter._rpc = fake_rpc
    events, cursor = await adapter.poll(cursor="0x1f")
    assert cursor == "0x20"
    assert len(events) == 1
    assert events[0].metadata["token0_address"] == token0
    assert events[0].metadata["token1_address"] == token1
    assert events[0].metadata["raw_delta"] == "-1000000"
    assert events[0].metadata["amount1_delta"] == "2000000"



@pytest.mark.asyncio
async def test_evm_reorg_rewinds_to_common_ancestor_and_exposes_tombstone_range():
    adapter = EVMLogCapitalAdapter(
        rpc_url="https://rpc.example",
        chain_id="1",
        contracts=["0x" + "1" * 40],
        confirmations=2,
        max_block_range=10,
        max_reorg_depth=8,
    )
    old_hashes = {str(number): "0x" + f"{number:064x}" for number in range(93, 101)}
    cursor = json.dumps(
        {"version": 2, "head_block": 100, "recent_hashes": old_hashes}
    )
    new_hash = "0x" + "f" * 64

    async def fake_rpc(method, params):
        if method == "eth_blockNumber":
            return "0x66"
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            return {
                "timestamp": hex(1_700_000_000 + number),
                "hash": new_hash if number == 100 else old_hashes.get(
                    str(number), "0x" + f"{number:064x}"
                ),
            }
        if method == "eth_getLogs":
            assert params[0]["fromBlock"] == "0x64"
            assert params[0]["toBlock"] == "0x64"
            return []
        raise AssertionError(method)

    adapter._rpc = fake_rpc
    events, next_cursor = await adapter.poll(cursor=cursor)
    assert events == []
    assert adapter.last_reorg["from_block"] == 100
    assert adapter.last_reorg["to_block"] == 100
    assert adapter.last_reorg["canonical_hashes"] == {100: new_hash}
    assert json.loads(next_cursor)["head_block"] == 100
