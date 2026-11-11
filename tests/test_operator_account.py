from copy import deepcopy

import pytest

from aitos.exchange.operator_account import (
    BinanceOperatorAccount,
    AccountObservationUnavailable,
)


def responses():
    return {
        "/fapi/v3/account": dict(
            totalWalletBalance="100.00000001",
            totalUnrealizedProfit="-1",
            totalMarginBalance="99.00000001",
            totalInitialMargin="10",
            totalMaintMargin="1",
            availableBalance="89.00000001",
            apiKey="must-not-escape",
        ),
        "/fapi/v3/positionRisk": [
            dict(
                symbol="BTCUSDT",
                positionSide="LONG",
                positionAmt="0.01",
                markPrice="9900",
                entryPrice="10000",
                unRealizedProfit="-1",
                accountId="must-not-escape",
            )
        ],
    }


async def test_signed_read_paths_and_exact_values_only():
    data, calls = responses(), []

    async def get(path, params):
        calls.append((path, params))
        return deepcopy(data[path])

    observed = await BinanceOperatorAccount(get).observe()
    assert set(path for path, _ in calls) == set(data)
    assert all(params == {} for _, params in calls)
    assert observed["totals"]["totalMarginBalance"] == "99.00000001"
    assert observed["positions"][0]["markPrice"] == "9900"
    assert not observed["consistent_snapshot"]
    assert "must-not-escape" not in str(observed)
    assert observed["controls"] == "read_only"


@pytest.mark.parametrize(
    "failure", ["nan", "missing", "inconsistent", "duplicate", "badmark", "symbol"]
)
async def test_invalid_provider_data_fails_without_synthetic_balances(failure):
    data = responses()
    if failure == "nan":
        data["/fapi/v3/account"]["totalWalletBalance"] = "NaN"
    elif failure == "missing":
        del data["/fapi/v3/account"]["availableBalance"]
    elif failure == "inconsistent":
        data["/fapi/v3/account"]["totalMarginBalance"] = "100"
    elif failure == "duplicate":
        data["/fapi/v3/positionRisk"] *= 2
    elif failure == "badmark":
        data["/fapi/v3/positionRisk"][0]["markPrice"] = "0"
    else:
        data["/fapi/v3/positionRisk"][0]["symbol"] = "<script>"

    async def get(path, params):
        return data[path]

    with pytest.raises(AccountObservationUnavailable):
        await BinanceOperatorAccount(get).observe()


async def test_transport_failure_redacted():
    async def get(path, params):
        raise RuntimeError("sensitive-api-key")

    with pytest.raises(AccountObservationUnavailable) as error:
        await BinanceOperatorAccount(get).observe()
    assert "sensitive" not in str(error.value)


async def test_failed_request_cancels_sibling_and_releases_lock():
    import asyncio

    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def get(path, params):
        if path.endswith("account"):
            await started.wait()
            raise RuntimeError("synthetic failure")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monitor = BinanceOperatorAccount(get)
    with pytest.raises(AccountObservationUnavailable):
        await monitor.observe()
    assert cancelled.is_set()
    assert not monitor._lock.locked()
