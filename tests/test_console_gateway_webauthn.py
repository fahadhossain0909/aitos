import pytest
from aiohttp.test_utils import TestClient, TestServer

from aitos.console_gateway import GatewayConfig, create_gateway
from aitos.security.live_approval import LiveApprovalControlPlane

ORIGIN = "http://127.0.0.1:8780"
PASSWORD = "a-very-long-test-password-with-enough-entropy"


class FakeWebAuthn:
    rp_id = "127.0.0.1"
    origins = frozenset({ORIGIN})

    def __init__(self, credentials=0):
        self.credentials = credentials
        self.registration_verified = False
        self.bootstrap_consumed = set()

    def consume_enrollment_bootstrap_token(
        self, *, user_id, configured_token, supplied_token, now
    ):
        if (
            user_id in self.bootstrap_consumed
            or len(configured_token) < 32
            or supplied_token != configured_token
        ):
            return False
        self.bootstrap_consumed.add(user_id)
        return True

    def credential_count(self, *, user_id):
        assert user_id == "operator-1"
        return self.credentials

    def list_credentials(self, *, user_id):
        assert user_id == "operator-1"
        return [
            {
                "credential_id": f"credential-{index}",
                "transports": ["usb"],
                "device_type": "single_device",
                "backed_up": False,
                "created_at": 100 + index,
                "last_used_at": None,
            }
            for index in range(self.credentials)
        ]

    def registration_options(self, *, user_id, now):
        assert user_id == "operator-1"
        return {"challenge": "registration-challenge", "rp": {"id": self.rp_id}}

    def verify_registration(self, *, user_id, credential, now):
        assert user_id == "operator-1"
        assert credential == {"id": "credential"}
        self.credentials += 1
        self.registration_verified = True
        return "credential-id"

    def authentication_options(self, *, user_id, now):
        assert user_id == "operator-1"
        assert self.credentials > 0
        return {"challenge": "assertion-challenge", "rpId": self.rp_id}

    def verify_assertion(
        self, *, assertion, challenge, expected_origin, expected_rp_id,
        require_user_verification
    ):
        assert assertion == b'{"id":"credential"}'
        assert challenge == "assertion-challenge"
        assert expected_origin == self.origins
        assert expected_rp_id == self.rp_id
        assert require_user_verification is True
        return {
            "verified": True,
            "user_id": "operator-1",
            "user_verified": True,
            "origin": ORIGIN,
            "rp_id": self.rp_id,
        }


@pytest.fixture
async def gateway(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    config = GatewayConfig(
        ORIGIN,
        "http://127.0.0.1:8765",
        dist,
        workspace_file=tmp_path / "workspace.json",
    )
    rp = FakeWebAuthn()
    app = create_gateway(
        config, password=PASSWORD, webauthn_rp=rp, operator_id="operator-1"
    )
    client = TestClient(TestServer(app), headers={"Host": "127.0.0.1:8780"})
    await client.start_server()
    try:
        yield client, rp
    finally:
        await client.close()


async def test_password_login_requires_enrollment_then_webauthn_assertion(gateway):
    client, rp = gateway
    login = await client.post(
        "/operator/v1/login",
        json={"password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert login.status == 200
    assert (await login.json())["mfa_enrollment_required"] is True
    assert "aitos_local_operator_pending" in login.cookies
    assert (
        await (await client.get("/operator/v1/session")).json()
    )["authenticated"] is False

    enrolled = await client.post(
        "/operator/v1/mfa/register",
        json={"credential": {"id": "credential"}},
        headers={"Origin": ORIGIN},
    )
    assert enrolled.status == 200
    options = await enrolled.json()
    assert options["mfa_required"] is True
    assert rp.registration_verified is True

    verified = await client.post(
        "/operator/v1/mfa/verify",
        json={
            "challenge": "assertion-challenge",
            "assertion": {"id": "credential"},
        },
        headers={"Origin": ORIGIN},
    )
    assert verified.status == 200
    result = await verified.json()
    assert result["authenticated"] is True
    assert result["mfa"] is True
    session = await (await client.get("/operator/v1/session")).json()
    assert session["authenticated"] is True
    assert session["identity"] == "operator-1"
    assert session["mfa"] is True
    assert "aitos_local_operator_pending" not in verified.cookies


async def test_existing_credential_login_never_issues_session_before_assertion(
    tmp_path,
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    rp = FakeWebAuthn(credentials=1)
    config = GatewayConfig(
        ORIGIN,
        "http://127.0.0.1:8765",
        dist,
        workspace_file=tmp_path / "workspace.json",
    )
    app = create_gateway(
        config, password=PASSWORD, webauthn_rp=rp, operator_id="operator-1"
    )
    client = TestClient(TestServer(app), headers={"Host": "127.0.0.1:8780"})
    await client.start_server()
    try:
        response = await client.post(
            "/operator/v1/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert (await response.json())["mfa_required"] is True
        assert (
            await (await client.get("/operator/v1/session")).json()
        )["authenticated"] is False
        assert "aitos_local_operator" not in response.cookies
    finally:
        await client.close()


async def test_mfa_enrollment_requires_live_password_preauthentication(gateway):
    client, _ = gateway
    response = await client.post(
        "/operator/v1/mfa/register",
        json={"credential": {"id": "credential"}},
        headers={"Origin": ORIGIN},
    )
    assert response.status == 401


async def test_approval_desk_requires_rbac_and_webauthn_step_up(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    rp = FakeWebAuthn(credentials=1)
    approval = LiveApprovalControlPlane(
        tmp_path / "approvals.sqlite",
        signing_key=b"test-signing-key-material-at-least-32-bytes",
        allowed_origins={ORIGIN},
        rp_id=rp.rp_id,
        role_authorizer=lambda user, role: user == "operator-1" and role == "live_trader",
    )
    config = GatewayConfig(
        ORIGIN,
        "http://127.0.0.1:8765",
        dist,
        workspace_file=tmp_path / "workspace.json",
    )
    app = create_gateway(
        config,
        password=PASSWORD,
        webauthn_rp=rp,
        approval_control_plane=approval,
        operator_id="operator-1",
    )
    client = TestClient(TestServer(app), headers={"Host": "127.0.0.1:8780"})
    await client.start_server()
    try:
        login = await client.post(
            "/operator/v1/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert (await login.json())["mfa_required"] is True
        verified = await client.post(
            "/operator/v1/mfa/verify",
            json={
                "challenge": "assertion-challenge",
                "assertion": {"id": "credential"},
            },
            headers={"Origin": ORIGIN},
        )
        assert verified.status == 200
        capabilities = await client.get("/operator/v1/approval/capabilities")
        assert (await capabilities.json())["enabled"] is True

        requested = await client.post(
            "/operator/v1/approval/challenge",
            json={"action": {"kind": "risk-limit-change", "limit": "0.25"}},
            headers={"Origin": ORIGIN},
        )
        assert requested.status == 200
        challenge = await requested.json()
        assert len(challenge["action_digest"]) == 64
        assert challenge["options"]["challenge"] == "assertion-challenge"

        issued = await client.post(
            "/operator/v1/approval/approve",
            json={
                "challenge_id": challenge["challenge_id"],
                "assertion": {"id": "credential"},
            },
            headers={"Origin": ORIGIN},
        )
        assert issued.status == 200
        token = await issued.json()
        assert token["user_id"] == "operator-1"
        assert token["action_digest"] == challenge["action_digest"]
        assert "signature" not in token
        assert token["expires_at"] > 0

        replay = await client.post(
            "/operator/v1/approval/approve",
            json={
                "challenge_id": challenge["challenge_id"],
                "assertion": {"id": "credential"},
            },
            headers={"Origin": ORIGIN},
        )
        assert replay.status == 403
    finally:
        await client.close()


async def test_remote_first_enrollment_requires_out_of_band_bootstrap_token(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    origin = "https://console.example"
    rp = FakeWebAuthn(credentials=0)
    rp.rp_id = "console.example"
    rp.origins = frozenset({origin})
    config = GatewayConfig(
        origin,
        "http://127.0.0.1:8765",
        dist,
        workspace_file=tmp_path / "workspace.json",
    )
    app = create_gateway(
        config,
        password=PASSWORD,
        webauthn_rp=rp,
        operator_id="operator-1",
        webauthn_enrollment_token="one-time-bootstrap-token-with-256-bit-minimum",
    )
    client = TestClient(TestServer(app), headers={"Host": "console.example"})
    await client.start_server()
    try:
        denied = await client.post(
            "/operator/v1/login",
            json={"password": PASSWORD},
            headers={"Origin": origin},
        )
        assert denied.status == 403
        assert "aitos_local_operator_pending" not in denied.cookies

        allowed = await client.post(
            "/operator/v1/login",
            json={
                "password": PASSWORD,
                "enrollment_token": "one-time-bootstrap-token-with-256-bit-minimum",
            },
            headers={"Origin": origin},
        )
        assert allowed.status == 200
        assert (await allowed.json())["mfa_enrollment_required"] is True
        replay = await client.post(
            "/operator/v1/login",
            json={
                "password": PASSWORD,
                "enrollment_token": "one-time-bootstrap-token-with-256-bit-minimum",
            },
            headers={"Origin": origin},
        )
        assert replay.status == 403
    finally:
        await client.close()


def test_remote_gateway_cannot_disable_webauthn(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    config = GatewayConfig(
        "https://console.example",
        "http://127.0.0.1:8765",
        dist,
    )
    with pytest.raises(ValueError, match="WebAuthn MFA is mandatory"):
        create_gateway(config, password=PASSWORD)


async def test_gateway_authenticates_distinct_operator_identities(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    identities = {
        "operator-a": "operator-a-password-with-sufficient-entropy",
        "operator-b": "operator-b-password-with-sufficient-entropy",
    }
    config = GatewayConfig(
        ORIGIN,
        "http://127.0.0.1:8765",
        dist,
        workspace_file=tmp_path / "workspace.json",
    )
    app = create_gateway(
        config,
        password=None,
        identities=identities,
        operator_id="operator-a",
    )
    client = TestClient(TestServer(app), headers={"Host": "127.0.0.1:8780"})
    await client.start_server()
    try:
        denied = await client.post(
            "/operator/v1/login",
            json={"user_id": "operator-b", "password": identities["operator-a"]},
            headers={"Origin": ORIGIN},
        )
        assert denied.status == 401
        accepted = await client.post(
            "/operator/v1/login",
            json={"user_id": "operator-b", "password": identities["operator-b"]},
            headers={"Origin": ORIGIN},
        )
        assert accepted.status == 200
        session = await (await client.get("/operator/v1/session")).json()
        assert session["identity"] == "operator-b"
        assert session["default_identity"] == "operator-a"
    finally:
        await client.close()


async def test_additional_authenticator_requires_existing_webauthn_step_up(gateway):
    client, rp = gateway
    login = await client.post(
        "/operator/v1/login",
        json={"password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert (await login.json())["mfa_enrollment_required"] is True
    registered = await client.post(
        "/operator/v1/mfa/register",
        json={"credential": {"id": "credential"}},
        headers={"Origin": ORIGIN},
    )
    assert registered.status == 200
    verified = await client.post(
        "/operator/v1/mfa/verify",
        json={
            "challenge": "assertion-challenge",
            "assertion": {"id": "credential"},
        },
        headers={"Origin": ORIGIN},
    )
    assert verified.status == 200

    options = await client.post(
        "/operator/v1/mfa/add/options", headers={"Origin": ORIGIN}
    )
    assert options.status == 200
    auth_options = await options.json()
    step_up = await client.post(
        "/operator/v1/mfa/add/verify",
        json={
            "challenge": auth_options["options"]["challenge"],
            "assertion": {"id": "credential"},
        },
        headers={"Origin": ORIGIN},
    )
    assert step_up.status == 200
    registration_options = await step_up.json()
    assert registration_options["options"]["challenge"] == "registration-challenge"

    finish = await client.post(
        "/operator/v1/mfa/add/finish",
        json={"credential": {"id": "credential"}},
        headers={"Origin": ORIGIN},
    )
    assert finish.status == 200
    inventory = await client.get("/operator/v1/mfa/credentials")
    assert (await inventory.json())["credentials"]
