import pytest

from aitos.security.live_approval import (
    ApprovalDenied,
    LiveApprovalControlPlane,
)


def verifier(**kwargs):
    return {
        "verified": True,
        "user_id": "alice",
        "user_verified": True,
        "origin": "https://console.example",
        "rp_id": "console.example",
    }


def setup(tmp_path):
    return LiveApprovalControlPlane(
        tmp_path / "approval.sqlite",
        signing_key=b"x" * 32,
        allowed_origins={"https://console.example"},
        rp_id="console.example",
        ttl_seconds=60,
        role_authorizer=lambda user, role: user == "alice" and role == "live_trader",
    )


def test_webauthn_approval_is_bound_to_action_and_single_use(tmp_path):
    cp = setup(tmp_path)
    action = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "qty": "0.01",
        "environment": "testnet",
    }
    challenge = cp.issue_challenge(
        user_id="alice", action=action, now=100
    )
    token = cp.approve(
        challenge, assertion=b"authenticator-assertion", verifier=verifier, now=101
    )
    assert cp.consume(token, action=action, now=102) == "alice"
    with pytest.raises(ApprovalDenied, match="consumed"):
        cp.consume(token, action=action, roles={"live_trader"}, now=103)


def test_rbac_and_action_mutation_are_denied(tmp_path):
    cp = setup(tmp_path)
    action = {"symbol": "BTCUSDT", "side": "BUY"}
    with pytest.raises(ApprovalDenied):
        cp.issue_challenge(user_id="alice", action=action)
    challenge = cp.issue_challenge(
        user_id="alice", action=action, roles={"live_trader"}, now=100
    )
    token = cp.approve(challenge, assertion=b"assertion", verifier=verifier, now=101)
    with pytest.raises(ApprovalDenied, match="mismatched"):
        cp.consume(
            token,
            action={"symbol": "ETHUSDT", "side": "BUY"},
            now=102,
        )


def test_unverified_or_wrong_origin_assertions_fail_closed(tmp_path):
    cp = setup(tmp_path)
    challenge = cp.issue_challenge(
        user_id="alice", action={"x": 1}, roles={"live_trader"}, now=100
    )

    def bad(**kwargs):
        return {
            "verified": False,
            "user_id": "alice",
            "user_verified": False,
            "origin": "https://evil.example",
            "rp_id": "console.example",
        }

    with pytest.raises(ApprovalDenied):
        cp.approve(challenge, assertion=b"bad", verifier=bad, now=101)


def test_expired_challenge_cannot_issue_token(tmp_path):
    cp = setup(tmp_path)
    challenge = cp.issue_challenge(
        user_id="alice", action={"x": 1}, roles={"live_trader"}, now=100
    )
    with pytest.raises(ApprovalDenied, match="expired"):
        cp.approve(challenge, assertion=b"assertion", verifier=verifier, now=200)
