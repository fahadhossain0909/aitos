import pytest
from aiohttp.test_utils import TestClient, TestServer

from aitos.console_gateway import GatewayConfig, create_gateway

ORIGIN = "http://127.0.0.1:8780"
PASSWORD = "a-very-long-test-password-with-enough-entropy"


class FakeWebAuthn:
    rp_id = "127.0.0.1"
    origins = frozenset({ORIGIN})

    def __init__(self, credentials=0):
        self.credentials = credentials
        self.registration_verified = False

    def credential_count(self, *, user_id):
        assert user_id == "operator-1"
        return self.credentials

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
