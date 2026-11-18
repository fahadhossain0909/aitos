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
    assert (
        evidence[mint][0]["ownership_scope"] == "token_account_owner_from_rpc_metadata"
    )


def test_solana_native_inner_transfer_is_attached_as_evidence_only():
    wallet = "wallet-owner"
    counterparty = "counterparty-owner"
    message = {
        "instructions": [],
        "innerInstructions": [
            {
                "index": 0,
                "instructions": [
                    {
                        "program": "system",
                        "parsed": {
                            "type": "transfer",
                            "info": {
                                "source": wallet,
                                "destination": counterparty,
                                "lamports": 250000000,
                            },
                        },
                    }
                ],
            }
        ],
    }
    evidence = SolanaAddressActivityAdapter._system_transfer_evidence(message, wallet)
    assert len(evidence) == 1
    assert evidence[0]["counterparty"] == counterparty
    assert evidence[0]["raw_lamports"] == "250000000"
    assert evidence[0]["is_cpi_or_inner_instruction"] is True
    assert evidence[0]["evidence_only"] is True


def test_solana_native_transfer_evidence_ignores_unrelated_or_unparsed_instructions():
    message = {
        "instructions": [
            {
                "program": "system",
                "parsed": {
                    "type": "transfer",
                    "info": {
                        "source": "other",
                        "destination": "elsewhere",
                        "lamports": 10,
                    },
                },
            },
            {
                "program": "system",
                "parsed": {
                    "type": "transfer",
                    "info": {
                        "source": "wallet",
                        "destination": "other",
                        "lamports": "bad",
                    },
                },
            },
            {"program": "system", "data": "opaque"},
        ]
    }
    assert (
        SolanaAddressActivityAdapter._system_transfer_evidence(message, "wallet") == []
    )


def test_solana_stake_delegate_and_withdraw_are_evidence_not_extra_flow():
    wallet = "wallet-owner"
    stake_account = "stake-account"
    vote_account = "vote-account"
    message = {
        "instructions": [
            {
                "program": "stake",
                "parsed": {
                    "type": "delegate",
                    "info": {
                        "stakeAccount": stake_account,
                        "voteAccount": vote_account,
                        "authority": wallet,
                    },
                },
            }
        ],
        "innerInstructions": [
            {
                "index": 0,
                "instructions": [
                    {
                        "program": "stake",
                        "parsed": {
                            "type": "withdraw",
                            "info": {
                                "stakeAccount": stake_account,
                                "destination": wallet,
                                "withdrawAuthority": wallet,
                                "lamports": 5000000,
                            },
                        },
                    }
                ],
            }
        ],
    }
    evidence = SolanaAddressActivityAdapter._stake_instruction_evidence(message, wallet)
    assert len(evidence) == 2
    assert evidence[0]["instruction_type"] == "delegate"
    assert evidence[1]["instruction_type"] == "withdraw"
    assert evidence[1]["raw_lamports"] == "5000000"
    assert evidence[1]["is_cpi_or_inner_instruction"] is True
    assert all(item["evidence_only"] is True for item in evidence)
    assert all(
        item["ownership_scope"]
        == "instruction_account_reference_not_beneficial_owner_proof"
        for item in evidence
    )


def test_solana_stake_evidence_ignores_unrelated_wallets_and_unparsed_instructions():
    message = {
        "instructions": [
            {
                "program": "stake",
                "parsed": {
                    "type": "delegate",
                    "info": {"stakeAccount": "other", "voteAccount": "vote"},
                },
            },
            {"program": "stake", "data": "opaque"},
            {
                "program": "system",
                "parsed": {
                    "type": "transfer",
                    "info": {"source": "wallet", "destination": "other", "lamports": 1},
                },
            },
        ]
    }
    assert (
        SolanaAddressActivityAdapter._stake_instruction_evidence(message, "wallet")
        == []
    )
