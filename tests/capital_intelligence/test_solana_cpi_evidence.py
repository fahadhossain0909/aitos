from aitos.capital_intelligence.solana import SolanaAddressActivityAdapter


def test_solana_spl_inner_instruction_is_attached_as_evidence_not_extra_flow():
    wallet = "wallet-owner"
    counterparty = "counterparty-owner"
    source_account = "source-token-account"
    destination_account = "destination-token-account"
    mint = "token-mint"
    message = {
        "instructions": [],
    }
    meta = {
        "preTokenBalances": [
            {"accountIndex": 0, "owner": wallet, "mint": mint},
            {"accountIndex": 1, "owner": counterparty, "mint": mint},
        ],
        "postTokenBalances": [],
        "innerInstructions": [
            {
                "index": 0,
                "instructions": [
                    {
                        "program": "spl-token",
                        "parsed": {
                            "type": "transferChecked",
                            "info": {
                                "source": source_account,
                                "destination": destination_account,
                                "mint": mint,
                                "authority": wallet,
                                "tokenAmount": {"amount": "1250000", "decimals": 6},
                            },
                        },
                    }
                ],
            }
        ],
    }
    evidence = SolanaAddressActivityAdapter._spl_transfer_evidence(
        message,
        meta,
        wallet,
        [source_account, destination_account],
    )
    assert len(evidence[mint]) == 1
    assert evidence[mint][0]["counterparty_owner"] == counterparty
    assert evidence[mint][0]["raw_amount"] == "1250000"
    assert evidence[mint][0]["is_cpi_or_inner_instruction"] is True
    assert evidence[mint][0]["ownership_scope"] == "token_account_owner_from_rpc_metadata"
