from datetime import datetime, timezone
import json
import pytest

from aitos.capital_intelligence.evm import EVMLogCapitalAdapter, TRANSFER_TOPIC
from aitos.capital_intelligence.dex import (
    EvmDexSwapAdapter,
    V3_SWAP_TOPIC,
    V4_SWAP_TOPIC,
)
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
    assert cursor_state["head_block"] == 0x1E
    assert len(cursor_state["recent_hashes"]) == 0x1E + 1
    assert len(events) == 1
    assert events[0].event_time == datetime.fromtimestamp(1_700_000_000, timezone.utc)
    assert events[0].metadata["raw_amount"] == str(raw_amount)
    assert events[0].metadata["flow_sign"] == -1
    assert events[0].metadata["counterparty_identity_kind"] == "evm_address"


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
        "blockHash": "0x" + f"{0x20:064x}",
        "address": pool,
    }

    async def fake_rpc(method, params):
        if method == "eth_blockNumber":
            return "0x22"
        if method == "eth_getLogs":
            return [log]
        if method == "eth_call":
            return (
                "0x"
                + "0" * 24
                + (token0 if params[0]["data"] == "0x0dfe1681" else token1)[2:]
            )
        if method == "eth_getBlockByNumber":
            return {
                "timestamp": hex(1_700_000_000),
                "hash": "0x" + f"{int(params[0], 16):064x}",
            }
        raise AssertionError(method)

    adapter._rpc = fake_rpc
    events, cursor = await adapter.poll(cursor="0x1f")
    assert json.loads(cursor)["head_block"] == 0x20
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
    cursor = json.dumps({"version": 2, "head_block": 100, "recent_hashes": old_hashes})
    new_hash = "0x" + "f" * 64

    async def fake_rpc(method, params):
        if method == "eth_blockNumber":
            return "0x66"
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            return {
                "timestamp": hex(1_700_000_000 + number),
                "hash": (
                    new_hash
                    if number == 100
                    else old_hashes.get(str(number), "0x" + f"{number:064x}")
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


@pytest.mark.asyncio
async def test_dex_reorg_uses_common_ancestor_and_emits_reconciliation_range():
    pool = "0x" + "1" * 40
    adapter = EvmDexSwapAdapter(
        rpc_url="https://rpc.example",
        chain_id="1",
        pools={pool: "uniswap_v3"},
        confirmations=2,
        max_block_range=10,
        max_reorg_depth=2,
    )
    old_hashes = {str(number): "0x" + f"{number:064x}" for number in (98, 99, 100)}
    cursor = json.dumps({"version": 2, "head_block": 100, "recent_hashes": old_hashes})
    replacement_hash = "0x" + "f" * 64

    async def fake_rpc(method, params):
        if method == "eth_blockNumber":
            return "0x66"
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            return {
                "timestamp": hex(1_700_000_000 + number),
                "hash": replacement_hash if number == 100 else old_hashes[str(number)],
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
    assert adapter.last_reorg["canonical_hashes"] == {100: replacement_hash}
    assert json.loads(next_cursor)["head_block"] == 100


@pytest.mark.asyncio
async def test_dex_reorg_deeper_than_retained_window_fails_closed():
    pool = "0x" + "1" * 40
    adapter = EvmDexSwapAdapter(
        rpc_url="https://rpc.example",
        chain_id="1",
        pools={pool: "uniswap_v3"},
        confirmations=2,
        max_block_range=10,
        max_reorg_depth=2,
    )
    old_hashes = {str(number): "0x" + f"{number:064x}" for number in (98, 99, 100)}
    cursor = json.dumps({"version": 2, "head_block": 100, "recent_hashes": old_hashes})

    async def fake_rpc(method, params):
        if method == "eth_blockNumber":
            return "0x66"
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            return {"timestamp": hex(1_700_000_000), "hash": "0x" + "f" * 64}
        raise AssertionError(method)

    adapter._rpc = fake_rpc
    with pytest.raises(RuntimeError, match="exceeds retained canonical window"):
        await adapter.poll(cursor=cursor)


@pytest.mark.asyncio
async def test_v4_swap_decodes_signed_deltas_by_pool_id_and_manager():
    manager = "0x" + "1" * 40
    pool_id = "0x" + "a" * 64
    token0 = "0x" + "0" * 40
    token1 = "0x" + "2" * 40
    actor = "0x" + "3" * 40
    adapter = EvmDexSwapAdapter(
        rpc_url="https://rpc.example",
        chain_id="1",
        pools={manager: "uniswap_v4"},
        v4_pool_tokens={pool_id: {"token0": token0, "token1": token1}},
        confirmations=2,
        max_block_range=10,
    )
    amount0 = (-125_000) % (1 << 128)
    amount1 = 250_000
    words = [
        amount0,
        amount1,
        2**96,
        5_000_000,
        (-12) % (1 << 24),
        3000,
    ]
    log = {
        "address": manager,
        "topics": [
            V4_SWAP_TOPIC,
            pool_id,
            "0x" + "0" * 24 + actor[2:],
        ],
        "data": "0x" + "".join(f"{value:064x}" for value in words),
        "blockNumber": "0x20",
        "transactionHash": "0x" + "b" * 64,
        "logIndex": "0x1",
        "blockHash": "0x" + f"{0x20:064x}",
    }

    async def fake_rpc(method, params):
        if method == "eth_blockNumber":
            return "0x22"
        if method == "eth_getLogs":
            assert params[0]["address"] == [manager]
            assert V4_SWAP_TOPIC in params[0]["topics"][0]
            return [log]
        if method == "eth_getBlockByNumber":
            return {
                "timestamp": hex(1_700_000_000),
                "hash": "0x" + f"{int(params[0], 16):064x}",
            }
        raise AssertionError(method)

    adapter._rpc = fake_rpc
    events, cursor = await adapter.poll(cursor="0x1f")
    assert len(events) == 1
    event = events[0]
    assert event.actor_id == actor
    assert event.instrument == "uniswap_v4"
    assert event.metadata["pool_id"] == pool_id
    assert event.metadata["pool_manager"] == manager
    assert event.metadata["token0_address"] == token0
    assert event.metadata["token1_address"] == token1
    assert event.metadata["amount0_delta"] == "-125000"
    assert event.metadata["amount1_delta"] == "250000"
    assert event.metadata["post_swap_tick"] == -12
    assert event.metadata["swap_fee_pips"] == 3000
    assert event.metadata["actor_is_pool_caller_not_verified_beneficial_owner"] is True
    assert json.loads(cursor)["head_block"] == 0x20


def test_v4_swap_requires_valid_pool_id_and_sorted_currency_mapping():
    manager = "0x" + "1" * 40
    pool_id = "0x" + "a" * 64
    with pytest.raises(ValueError, match="currencies"):
        EvmDexSwapAdapter(
            rpc_url="https://rpc.example",
            chain_id="1",
            pools={manager: "uniswap_v4"},
            v4_pool_tokens={
                pool_id: {
                    "token0": "0x" + "2" * 40,
                    "token1": "0x" + "1" * 40,
                }
            },
        )


@pytest.mark.asyncio
async def test_solana_stake_instruction_is_persisted_without_balance_delta():
    address = "stake-account"
    adapter = SolanaAddressActivityAdapter(
        rpc_url="https://solana.example",
        addresses=[address],
    )

    async def fake_rpc(method, params):
        if method == "getSignaturesForAddress":
            return [{"signature": "stake-signature"}]
        if method == "getTransaction":
            return {
                "slot": 99,
                "blockTime": 1_700_000_000,
                "transaction": {
                    "message": {
                        "accountKeys": [{"pubkey": address}],
                        "instructions": [
                            {
                                "program": "stake",
                                "parsed": {
                                    "type": "delegate",
                                    "info": {
                                        "stakeAccount": address,
                                        "voteAccount": "vote-account",
                                        "stakeAuthority": "wallet-authority",
                                    },
                                },
                            }
                        ],
                    }
                },
                "meta": {
                    "err": None,
                    "preBalances": [100],
                    "postBalances": [100],
                    "preTokenBalances": [],
                    "postTokenBalances": [],
                },
            }
        raise AssertionError(method)

    adapter._rpc = fake_rpc
    events, _ = await adapter.poll()
    assert len(events) == 1
    event = events[0]
    assert event.source_kind == "solana_stake_instruction_evidence"
    assert event.event_type == "stake_instruction_observed"
    assert event.quantity == 0
    assert event.unit == "instruction_evidence"
    assert event.usd_value is None
    assert event.metadata["evidence_only"] is True
    assert (
        event.metadata["stake_instruction_evidence"][0]["instruction_type"]
        == "delegate"
    )



@pytest.mark.asyncio
async def test_solana_cpi_system_and_stake_instructions_are_retained_as_evidence():
    address = "monitored-wallet"
    adapter = SolanaAddressActivityAdapter(
        rpc_url="https://solana.example",
        addresses=[address],
    )

    async def fake_rpc(method, params):
        if method == "getSignaturesForAddress":
            return [{"signature": "cpi-signature"}]
        if method == "getTransaction":
            return {
                "slot": 123,
                "blockTime": 1_700_000_000,
                "transaction": {
                    "message": {
                        "accountKeys": [{"pubkey": address}],
                        "instructions": [],
                    }
                },
                "meta": {
                    "err": None,
                    "preBalances": [100],
                    "postBalances": [95],
                    "preTokenBalances": [],
                    "postTokenBalances": [],
                    "innerInstructions": [
                        {
                            "index": 0,
                            "instructions": [
                                {
                                    "program": "system",
                                    "parsed": {
                                        "type": "transfer",
                                        "info": {
                                            "source": address,
                                            "destination": "counterparty-wallet",
                                            "lamports": 5,
                                        },
                                    },
                                },
                                {
                                    "program": "stake",
                                    "parsed": {
                                        "type": "delegate",
                                        "info": {
                                            "stakeAccount": address,
                                            "voteAccount": "vote-account",
                                            "stakeAuthority": "wallet-authority",
                                        },
                                    },
                                },
                            ],
                        }
                    ],
                },
            }
        raise AssertionError(method)

    adapter._rpc = fake_rpc
    events, _ = await adapter.poll()
    native = next(event for event in events if event.event_type == "sol_balance_change")
    assert len(native.metadata["system_transfer_evidence"]) == 1
    transfer = native.metadata["system_transfer_evidence"][0]
    assert transfer["counterparty"] == "counterparty-wallet"
    assert transfer["raw_lamports"] == "5"
    assert transfer["is_cpi_or_inner_instruction"] is True

    stake = next(
        event for event in events if event.event_type == "stake_instruction_observed"
    )
    assert (
        stake.metadata["stake_instruction_evidence"][0]["instruction_type"]
        == "delegate"
    )
    assert (
        stake.metadata["stake_instruction_evidence"][0][
            "is_cpi_or_inner_instruction"
        ]
        is True
    )
