import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from aitos.exchange.binance_usdm_paper_signer import (
    BinancePaperExchangeError,
    BinanceUSDMPaperAccountEvidenceProvider,
    BinanceUSDMPaperClient,
    BinanceUSDMPaperExecutionAdapter,
    PaperSignerRecoveryWorker,
)
from aitos.security.replay_store import SQLiteReplayStore
from aitos.security.signer_credentials import SignerCredentialSlots
from aitos.security.signing_policy import OrderIntentPolicy, validate_order_intent


NOW = 1_800_000_000_000


class MemoryVault:
    def __init__(self):
        self.values = {}

    def put(self, slot, secret):
        self.values[slot] = secret

    def get(self, slot):
        return self.values.get(slot)

    def delete(self, slot):
        return self.values.pop(slot, None) is not None


class FakeTransport:
    def __init__(
        self,
        *,
        hedge=False,
        income_count=2,
        fail_order=False,
        order_status="NEW",
        executed_qty="0",
    ):
        self.calls = []
        self.hedge = hedge
        self.income_count = income_count
        self.fail_order = fail_order
        self.order_status = order_status
        self.executed_qty = executed_qty

    def request(self, method, url, *, headers, timeout):
        self.calls.append((method, url, dict(headers), timeout))
        parsed = urlsplit(url)
        assert parsed.netloc == "testnet.binancefuture.com"
        if parsed.path.endswith("/fapi/v1/time"):
            return {"serverTime": NOW}
        if "signature" in dict(parse_qsl(parsed.query)):
            pairs = parse_qsl(parsed.query)
            signature = dict(pairs)["signature"]
            unsigned = [(key, value) for key, value in pairs if key != "signature"]
            expected = hmac.new(
                b"test-secret", urlencode(unsigned).encode(), hashlib.sha256
            ).hexdigest()
            assert signature == expected
            assert headers["X-MBX-APIKEY"] == "test-key"
        if parsed.path.endswith("/fapi/v2/account"):
            side = "LONG" if self.hedge else "BOTH"
            return {
                "canTrade": True,
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": "1000",
                        "availableBalance": "900",
                    }
                ],
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": side,
                        "positionAmt": "0.01",
                        "notional": "100",
                    }
                ],
            }
        if parsed.path.endswith("/fapi/v1/openOrders"):
            return [
                {
                    "symbol": "BTCUSDT",
                    "origQty": "0.001",
                    "price": "60000",
                    "type": "LIMIT",
                    "reduceOnly": False,
                }
            ]
        if parsed.path.endswith("/fapi/v1/income"):
            return [
                {
                    "incomeType": "REALIZED_PNL",
                    "income": "-10",
                    "time": NOW,
                    "tranId": 1,
                },
                {"incomeType": "COMMISSION", "income": "-1", "time": NOW, "tranId": 2},
                {"incomeType": "FUNDING_FEE", "income": "2", "time": NOW, "tranId": 3},
            ][: self.income_count]
        if parsed.path.endswith("/fapi/v1/order") and method == "GET":
            return {
                "orderId": 123,
                "clientOrderId": "aitos-testnet-001",
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.0005",
            }
        if parsed.path.endswith("/fapi/v1/order") and method == "POST":
            if self.fail_order:
                raise BinancePaperExchangeError("testnet_transport_unavailable")
            return {
                "orderId": 123,
                "clientOrderId": "aitos-testnet-001",
                "status": self.order_status,
                "executedQty": self.executed_qty,
            }
        raise AssertionError(f"unexpected endpoint: {parsed.path}")


def make_client(tmp_path, transport):
    slots = SignerCredentialSlots(MemoryVault())
    slots.save("paper-primary", key_id="test-key", api_secret="test-secret")
    return BinanceUSDMPaperClient(
        credentials=slots,
        account_alias="paper-primary",
        transport=transport,
        clock_ms=lambda: NOW,
    )


def test_account_evidence_uses_signed_testnet_account_and_position_data(tmp_path):
    transport = FakeTransport()
    client = make_client(tmp_path, transport)
    snapshot = BinanceUSDMPaperAccountEvidenceProvider(client=client).get_snapshot(
        "paper-primary", now_ms=NOW
    )
    assert snapshot.gross_exposure == Decimal("160.000")
    assert snapshot.symbol_exposure["BTCUSDT"] == Decimal("160.000")
    assert snapshot.positions["BTCUSDT"] == Decimal("0.01")
    assert snapshot.daily_loss == Decimal("9")
    assert snapshot.order_count_window == 1
    assert all(
        urlsplit(call[1]).netloc == "testnet.binancefuture.com"
        for call in transport.calls
    )


def test_hedge_mode_and_incomplete_income_fail_closed(tmp_path):
    hedge = make_client(tmp_path, FakeTransport(hedge=True))
    with pytest.raises(BinancePaperExchangeError, match="hedge_mode"):
        BinanceUSDMPaperAccountEvidenceProvider(client=hedge).get_snapshot(
            "paper-primary", now_ms=NOW
        )

    class FullIncome(FakeTransport):
        def request(self, method, url, *, headers, timeout):
            if urlsplit(url).path.endswith("/fapi/v1/income"):
                self.calls.append((method, url, dict(headers), timeout))
                return [{"incomeType": "REALIZED_PNL", "income": "0"}] * 1000
            return super().request(method, url, headers=headers, timeout=timeout)

    full = make_client(tmp_path, FullIncome())
    with pytest.raises(BinancePaperExchangeError, match="daily_income_incomplete"):
        BinanceUSDMPaperAccountEvidenceProvider(client=full).get_snapshot(
            "paper-primary", now_ms=NOW
        )


def _intent():
    policy = OrderIntentPolicy(
        account_alias="paper-primary",
        allowed_symbols=frozenset({"BTCUSDT"}),
        max_order_notional=Decimal("1000"),
    )
    return validate_order_intent(
        {
            "schema_version": 1,
            "intent_id": "intent-testnet-001",
            "nonce": "AAECAwQFBgcICQoLDA0ODw",
            "issued_at_ms": NOW - 100,
            "expires_at_ms": NOW + 10_000,
            "account_alias": "paper-primary",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": "0.001",
            "limit_price": "65000",
            "reduce_only": False,
            "client_order_id": "aitos-testnet-001",
        },
        policy=policy,
        now_ms=NOW,
    )


def test_signer_dispatches_limit_order_only_to_testnet(tmp_path):
    transport = FakeTransport()
    adapter = BinanceUSDMPaperExecutionAdapter(client=make_client(tmp_path, transport))
    receipt = adapter.execute(_intent())
    assert adapter.paper_only is True
    assert adapter.testnet_only is True
    assert receipt.status == "accepted"
    post = [call for call in transport.calls if call[0] == "POST"]
    assert len(post) == 1
    assert urlsplit(post[0][1]).netloc == "testnet.binancefuture.com"


def test_order_dispatch_timeout_is_ambiguous_not_rejected(tmp_path):
    adapter = BinanceUSDMPaperExecutionAdapter(
        client=make_client(tmp_path, FakeTransport(fail_order=True))
    )
    receipt = adapter.execute(_intent())
    assert receipt.status == "unknown"
    assert receipt.outcome_code == "testnet_dispatch_ambiguous"


def test_recovery_worker_reconciles_dispatching_order_without_resubmission(tmp_path):
    transport = FakeTransport()
    adapter = BinanceUSDMPaperExecutionAdapter(client=make_client(tmp_path, transport))
    with SQLiteReplayStore(tmp_path / "replay.sqlite3") as replay:
        intent = _intent()
        replay.reserve(intent, now_ms=NOW)
        replay.mark_dispatching(intent.intent_id, now_ms=NOW + 1)
        worker = PaperSignerRecoveryWorker(replay_store=replay, adapter=adapter)
        result = worker.run_once(now_ms=NOW + 2)
        assert result == {"resolved": 1, "unresolved": 0}
        reconciled = replay.get(intent.intent_id)
        assert reconciled.status == "accepted"
        assert reconciled.outcome_code == "testnet_order_reconciled"
        assert reconciled.symbol == "BTCUSDT"
        assert not any(call[0] == "POST" for call in transport.calls)


def test_recovery_worker_keeps_unknown_when_exchange_lookup_is_unavailable(tmp_path):
    class LookupUnavailable(FakeTransport):
        def request(self, method, url, *, headers, timeout):
            if urlsplit(url).path.endswith("/fapi/v1/order") and method == "GET":
                raise BinancePaperExchangeError("simulated_lookup_unavailable")
            return super().request(method, url, headers=headers, timeout=timeout)

    adapter = BinanceUSDMPaperExecutionAdapter(
        client=make_client(tmp_path, LookupUnavailable())
    )
    with SQLiteReplayStore(tmp_path / "replay.sqlite3") as replay:
        intent = _intent()
        replay.reserve(intent, now_ms=NOW)
        replay.mark_dispatching(intent.intent_id, now_ms=NOW + 1)
        result = PaperSignerRecoveryWorker(
            replay_store=replay, adapter=adapter
        ).run_once(now_ms=NOW + 2)
        assert result == {"resolved": 0, "unresolved": 1}
        assert replay.get(intent.intent_id).status == "dispatching"



def test_canceled_order_with_partial_fill_is_not_reported_as_rejected(tmp_path):
    adapter = BinanceUSDMPaperExecutionAdapter(
        client=make_client(
            tmp_path,
            FakeTransport(order_status="CANCELED", executed_qty="0.0005"),
        )
    )
    receipt = adapter.execute(_intent())
    assert receipt.status == "accepted"
    assert receipt.outcome_code == "testnet_order_partially_executed"
