import pytest

from aitos.capital_intelligence.evm_token_metadata import EvmTokenMetadataResolver


def _abi_string(value: str) -> str:
    raw = value.encode()
    padded = raw + b"\x00" * ((32 - len(raw) % 32) % 32)
    return "0x" + (32).to_bytes(32, "big").hex() + len(raw).to_bytes(32, "big").hex() + padded.hex()


@pytest.mark.asyncio
async def test_resolves_erc20_metadata_without_trusting_symbol_as_price_pair():
    resolver = EvmTokenMetadataResolver(
        rpc_url="https://rpc.example", chain_id="1"
    )

    async def fake_rpc(method, params):
        selector = params[0]["data"]
        if selector == "0x313ce567":
            return "0x" + (6).to_bytes(32, "big").hex()
        if selector == "0x95d89b41":
            return _abi_string("USDC")
        raise AssertionError(method)

    resolver._rpc = fake_rpc
    token = await resolver.resolve("0x" + "A" * 40)
    assert token.decimals == 6
    assert token.symbol == "USDC"
    assert token.asset_id == "evm:1:" + "0x" + "a" * 40
    assert token.price_symbol is None


@pytest.mark.asyncio
async def test_metadata_discovery_rejects_bad_decimals_and_duplicate_allowlist():
    resolver = EvmTokenMetadataResolver(
        rpc_url="https://rpc.example", chain_id="1"
    )
    resolver._rpc = lambda *args: None
    with pytest.raises(ValueError, match="1-500"):
        await resolver.resolve_many([])
    address = "0x" + "1" * 40
    with pytest.raises(ValueError, match="duplicate"):
        await resolver.resolve_many([address, address])
    with pytest.raises(ValueError, match="decimals"):
        resolver._decode_uint("0x" + (37).to_bytes(32, "big").hex())


def test_erc20_metadata_decoder_supports_bytes32_and_rejects_unbounded_text():
    assert EvmTokenMetadataResolver._decode_text("0x" + b"WBTC".ljust(32, b"\x00").hex()) == "WBTC"
    with pytest.raises(ValueError, match="safe bounds"):
        EvmTokenMetadataResolver._decode_text("0x" + "ab" * 4097)
