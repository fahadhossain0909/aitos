import hashlib
import re
from types import SimpleNamespace

import pytest
from aioresponses import aioresponses

from aitos.execution.binance_executor import TESTNET_URL
from aitos.live_trading import (
    confirm_live_trading,
    create_live_approval_control_plane,
    prepare_live_executor,
)
from aitos.security.live_approval import LiveApprovalControlPlane


def test_confirm_live_trading_blocks_mainnet_without_hardware_approval():
    with pytest.raises(SystemExit) as exc_info:
        confirm_live_trading(["BTCUSDT"], testnet=False, input_fn=lambda prompt: "")
    assert exc_info.value.code == 1


def test_executor_factory_shares_gateway_approval_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AITOS_WEBAUTHN_DB", str(tmp_path / "webauthn.sqlite"))
    monkeypatch.setenv("AITOS_WEBAUTHN_ORIGIN", "https://console.example")
    monkeypatch.setenv(
        "AITOS_APPROVAL_SIGNING_KEY",
        "shared-mainnet-approval-signing-key-at-least-32-bytes",
    )
    monkeypatch.setenv(
        "AITOS_OPERATOR_ROLE_MAP",
        '{"operator-1":["live_trader"]}',
    )
    first = create_live_approval_control_plane()
    action = {
        "environment": "binance-usdm-mainnet",
        "scope": "live:account-1",
        "client_order_id": "approved-order-1",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "quantity": "0.01",
        "reference_price": "90000",
        "order_type": "MARKET",
        "reduce_only": False,
        "owner_trade_id": "trade-1",
    }
    challenge = first.issue_challenge(
        user_id="operator-1",
        action=action,
        webauthn_challenge="server-issued-challenge",
    )
    token = first.approve(
        challenge,
        assertion=b"verified-webauthn-assertion",
        verifier=lambda **kwargs: {
            "verified": True,
            "user_id": "operator-1",
            "user_verified": True,
            "origin": "https://console.example",
            "rp_id": "console.example",
        },
    )
    second = create_live_approval_control_plane()
    assert second.consume(token, action=action) == "operator-1"


def test_live_mainnet_session_gate_requires_explicit_opt_in_and_authority(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("AITOS_ENABLE_MAINNET_EXECUTION", raising=False)
    authority = LiveApprovalControlPlane(
        tmp_path / "live-approvals.sqlite",
        signing_key=b"shared-mainnet-approval-signing-key-at-least-32-bytes",
        allowed_origins={"https://console.example"},
        rp_id="console.example",
        role_authorizer=lambda user, role: user == "operator-1"
        and role == "live_trader",
    )
    with pytest.raises(SystemExit):
        confirm_live_trading(
            ["BTCUSDT"],
            testnet=False,
            input_fn=lambda prompt: "unused",
            approval_authority=authority,
        )
    monkeypatch.setenv("AITOS_EXECUTION_SCOPE", "live:account-1")
    monkeypatch.setenv("AITOS_ENABLE_MAINNET_EXECUTION", "1")
    inputs = iter(["operator-1", "I APPROVE LIVE TRADING"])
    assert (
        confirm_live_trading(
            ["BTCUSDT"],
            testnet=False,
            input_fn=lambda prompt: next(inputs),
            approval_authority=authority,
        )
        == "operator-1"
    )


@pytest.mark.asyncio
async def test_mainnet_executor_uses_shared_approval_authority_without_connecting(
    tmp_path, monkeypatch
):
    authority = LiveApprovalControlPlane(
        tmp_path / "live-approvals.sqlite",
        signing_key=b"shared-mainnet-approval-signing-key-at-least-32-bytes",
        allowed_origins={"https://console.example"},
        rp_id="console.example",
        role_authorizer=lambda user, role: True,
    )
    settings = SimpleNamespace(
        binance=SimpleNamespace(
            testnet=False,
            execution_credentials=("mainnet-api-key", "mainnet-api-secret"),
            recv_window_ms=5000,
            hedge_mode=False,
        )
    )
    expected_scope = hashlib.sha256(b"False:mainnet-api-key").hexdigest()
    monkeypatch.setenv("AITOS_ENABLE_MAINNET_EXECUTION", "1")
    monkeypatch.setenv("AITOS_EXECUTION_SCOPE", expected_scope)

    async def no_network(executor, settings, symbols):
        return executor

    monkeypatch.setattr("aitos.live_trading._prepare_connected_executor", no_network)
    executor = await prepare_live_executor(
        settings,
        ["BTCUSDT"],
        approval_authority=authority,
    )
    assert executor._execution_scope == expected_scope
    assert executor._live_approval_authority is authority
    assert executor._live_approval_token_provider == authority.get_unconsumed_token


@pytest.mark.asyncio
async def test_mainnet_executor_rejects_wrong_account_scope_before_connect(
    tmp_path, monkeypatch
):
    authority = LiveApprovalControlPlane(
        tmp_path / "live-approvals.sqlite",
        signing_key=b"shared-mainnet-approval-signing-key-at-least-32-bytes",
        allowed_origins={"https://console.example"},
        rp_id="console.example",
        role_authorizer=lambda user, role: True,
    )
    settings = SimpleNamespace(
        binance=SimpleNamespace(
            testnet=False,
            execution_credentials=("mainnet-api-key", "mainnet-api-secret"),
            recv_window_ms=5000,
            hedge_mode=False,
        )
    )
    monkeypatch.setenv("AITOS_ENABLE_MAINNET_EXECUTION", "1")
    monkeypatch.setenv("AITOS_EXECUTION_SCOPE", "wrong-account-scope")
    with pytest.raises(SystemExit, match="AITOS_EXECUTION_SCOPE"):
        await prepare_live_executor(
            settings,
            ["BTCUSDT"],
            approval_authority=authority,
        )


def test_confirm_live_trading_succeeds_with_exact_phrase():
    inputs = iter(["fahad", "I APPROVE LIVE TRADING"])
    operator = confirm_live_trading(
        ["BTCUSDT"], testnet=True, input_fn=lambda prompt: next(inputs)
    )
    assert operator == "fahad"


def test_confirm_live_trading_exits_on_empty_operator():
    inputs = iter([""])
    with pytest.raises(SystemExit) as exc_info:
        confirm_live_trading(
            ["BTCUSDT"], testnet=True, input_fn=lambda prompt: next(inputs)
        )
    assert exc_info.value.code == 1


def test_confirm_live_trading_exits_on_wrong_confirmation_phrase():
    inputs = iter(["fahad", "yes I approve"])
    with pytest.raises(SystemExit) as exc_info:
        confirm_live_trading(
            ["BTCUSDT"], testnet=True, input_fn=lambda prompt: next(inputs)
        )
    assert exc_info.value.code == 1


def test_confirm_live_trading_case_sensitive_exact_match_required():
    inputs = iter(["fahad", "i approve live trading"])  # lowercase — should fail
    with pytest.raises(SystemExit):
        confirm_live_trading(
            ["BTCUSDT"], testnet=True, input_fn=lambda prompt: next(inputs)
        )


class FakeSettings:
    class _Binance:
        def __init__(self):
            self.api_key = "mainnet-key"
            self.api_secret = "mainnet-secret"
            self.futures_testnet_api_key = "test-key"
            self.futures_testnet_api_secret = "test-secret"
            self.testnet = True
            self.recv_window_ms = 5000
            self.hedge_mode = False

        @property
        def execution_credentials(self):
            if self.testnet:
                return self.futures_testnet_api_key, self.futures_testnet_api_secret
            return self.api_key, self.api_secret

    def __init__(self):
        self.binance = FakeSettings._Binance()


@pytest.mark.asyncio
async def test_prepare_live_executor_blocks_direct_mainnet_construction():
    settings = FakeSettings()
    settings.binance.testnet = False
    with pytest.raises(SystemExit) as exc_info:
        await prepare_live_executor(settings, ["BTCUSDT"])
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_prepare_live_executor_exits_without_credentials():
    settings = FakeSettings()
    settings.binance.futures_testnet_api_key = ""
    with pytest.raises(SystemExit) as exc_info:
        await prepare_live_executor(settings, ["BTCUSDT"])
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_prepare_live_executor_exits_on_hedge_mode_mismatch():
    settings = FakeSettings()  # configured hedge_mode=False
    with aioresponses() as m:
        m.get(
            re.compile(
                r"^" + re.escape(TESTNET_URL + "/fapi/v1/positionSide/dual") + r".*"
            ),
            payload={"dualSidePosition": True},  # account is actually in hedge mode
        )
        with pytest.raises(SystemExit) as exc_info:
            await prepare_live_executor(settings, ["BTCUSDT"])
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_prepare_live_executor_succeeds_and_loads_symbol_filters():

    settings = FakeSettings()
    exchange_info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "quantityPrecision": 3,
                "pricePrecision": 1,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                ],
            }
        ],
    }
    with aioresponses() as m:
        m.get(
            re.compile(
                r"^" + re.escape(TESTNET_URL + "/fapi/v1/positionSide/dual") + r".*"
            ),
            payload={"dualSidePosition": False},
        )
        m.get(
            "https://demo-fapi.binance.com/fapi/v1/exchangeInfo", payload=exchange_info
        )

        executor = await prepare_live_executor(settings, ["BTCUSDT"])

    assert "BTCUSDT" in executor._symbol_filters
    assert executor._symbol_filters["BTCUSDT"].step_size == 0.001

    await executor.close()
