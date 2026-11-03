import re

import pytest
from aioresponses import aioresponses

from aitos.execution.binance_executor import TESTNET_URL
from aitos.live_trading import confirm_live_trading, prepare_live_executor


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
            self.api_key = "test-key"
            self.api_secret = "test-secret"
            self.testnet = True
            self.recv_window_ms = 5000
            self.hedge_mode = False

    def __init__(self):
        self.binance = FakeSettings._Binance()


@pytest.mark.asyncio
async def test_prepare_live_executor_exits_without_credentials():
    settings = FakeSettings()
    settings.binance.api_key = ""
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
    from aitos.exchange.binance import REST_BASE_URL

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
        m.get(f"{REST_BASE_URL}/fapi/v1/exchangeInfo", payload=exchange_info)

        executor = await prepare_live_executor(settings, ["BTCUSDT"])

    assert "BTCUSDT" in executor._symbol_filters
    assert executor._symbol_filters["BTCUSDT"].step_size == 0.001

    await executor.close()
