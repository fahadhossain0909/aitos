import pytest

from aitos.security.live_approval import ApprovalDenied, LiveApprovalControlPlane


def verifier(**kwargs):
    return {
        "verified": True,
        "user_id": "alice",
        "user_verified": True,
        "origin": "https://console.example",
        "rp_id": "console.example",
    }


def setup(tmp_path, *, allowed=True):
    return LiveApprovalControlPlane(
        tmp_path / "approval.sqlite",
        signing_key=b"x" * 32,
        allowed_origins={"https://console.example"},
        rp_id="console.example",
        ttl_seconds=60,
        role_authorizer=lambda user, role: (
            allowed and user == "alice" and role == "live_trader"
        ),
    )


def test_webauthn_approval_is_bound_to_action_and_single_use(tmp_path):
    cp = setup(tmp_path)
    action = {"symbol": "BTCUSDT", "side": "BUY", "qty": "0.01"}
    challenge = cp.issue_challenge(user_id="alice", action=action, now=100)
    token = cp.approve(
        challenge, assertion=b"authenticator-assertion", verifier=verifier, now=101
    )
    assert cp.consume(token, action=action, now=102) == "alice"
    with pytest.raises(ApprovalDenied, match="consumed"):
        cp.consume(token, action=action, now=103)


def test_server_side_rbac_denies_unprivileged_user(tmp_path):
    cp = setup(tmp_path, allowed=False)
    with pytest.raises(ApprovalDenied, match="server-side RBAC"):
        cp.issue_challenge(user_id="alice", action={"symbol": "BTCUSDT"}, now=100)


def test_action_mutation_is_denied(tmp_path):
    cp = setup(tmp_path)
    action = {"symbol": "BTCUSDT", "side": "BUY"}
    challenge = cp.issue_challenge(user_id="alice", action=action, now=100)
    token = cp.approve(challenge, assertion=b"assertion", verifier=verifier, now=101)
    with pytest.raises(ApprovalDenied, match="mismatched"):
        cp.consume(token, action={"symbol": "ETHUSDT", "side": "BUY"}, now=102)


def test_unverified_or_wrong_origin_assertions_fail_closed(tmp_path):
    cp = setup(tmp_path)
    challenge = cp.issue_challenge(user_id="alice", action={"x": 1}, now=100)

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
    challenge = cp.issue_challenge(user_id="alice", action={"x": 1}, now=100)
    with pytest.raises(ApprovalDenied, match="expired"):
        cp.approve(challenge, assertion=b"assertion", verifier=verifier, now=200)


def test_role_authorizer_is_mandatory(tmp_path):
    with pytest.raises(ValueError, match="role_authorizer"):
        LiveApprovalControlPlane(
            tmp_path / "no-rbac.sqlite",
            signing_key=b"x" * 32,
            allowed_origins={"https://console.example"},
            rp_id="console.example",
        )


def test_signed_approval_is_shared_across_instances_and_consumed_once(tmp_path):
    path = tmp_path / "shared-approval.sqlite"
    first = LiveApprovalControlPlane(
        path,
        signing_key=b"s" * 32,
        allowed_origins={"https://console.example"},
        rp_id="console.example",
        role_authorizer=lambda user, role: user == "alice" and role == "live_trader",
    )
    executor_authority = LiveApprovalControlPlane(
        path,
        signing_key=b"s" * 32,
        allowed_origins={"https://console.example"},
        rp_id="console.example",
        role_authorizer=lambda user, role: user == "alice" and role == "live_trader",
    )
    action = {
        "environment": "binance-usdm-mainnet",
        "client_order_id": "aitos-approved-client-001",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "quantity": "0.01",
        "order_type": "MARKET",
        "reduce_only": False,
    }
    challenge = first.issue_challenge(user_id="alice", action=action, now=100)
    first.approve(
        challenge, assertion=b"authenticator-assertion", verifier=verifier, now=101
    )

    receipt = executor_authority.get_unconsumed_token(action, now=102)
    assert executor_authority.consume(receipt, action=action, now=102) == "alice"
    with pytest.raises(ApprovalDenied, match="consumed"):
        first.consume(receipt, action=action, now=103)
