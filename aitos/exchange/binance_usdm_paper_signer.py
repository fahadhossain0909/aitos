"""Signer-owned Binance USD-M Futures Testnet evidence and paper dispatch.

The endpoint is hard-pinned to Binance Futures Testnet. No production endpoint,
arbitrary base URL, withdrawal API, or live dispatch mode is available. Exchange
credentials are resolved only inside the signer process from its OS credential
store and are never returned in receipts or logs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from aitos.security.paper_processor import PaperExecutionReceipt
from aitos.security.replay_store import IntentReservation, SQLiteReplayStore
from aitos.security.risk_limits import AccountRiskSnapshot
from aitos.security.signer_credentials import SignerCredentialSlots
from aitos.security.signing_policy import ValidatedOrderIntent

_TESTNET_BASE = "https://testnet.binancefuture.com"
_MAX_RESPONSE_BYTES = 1_048_576
_INCOME_TYPES = frozenset({"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"})


class BinancePaperExchangeError(RuntimeError):
    """Sanitized Binance Futures Testnet transport or schema failure."""


class HttpTransport(Protocol):
    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], timeout: float
    ) -> object: ...


class UrllibJsonTransport:
    """Small bounded HTTPS transport with the platform's default CA validation."""

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], timeout: float
    ) -> object:
        request = Request(url, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            # Never surface the response body: exchange errors can echo request data.
            raise BinancePaperExchangeError("binance_testnet_http_rejected") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BinancePaperExchangeError(
                "binance_testnet_transport_unavailable"
            ) from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise BinancePaperExchangeError("binance_testnet_response_too_large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise BinancePaperExchangeError(
                "binance_testnet_response_invalid"
            ) from None


class BinanceUSDMPaperClient:
    """HMAC request client pinned to USD-M Futures Testnet."""

    def __init__(
        self,
        *,
        credentials: SignerCredentialSlots,
        account_alias: str,
        transport: HttpTransport | None = None,
        timeout_seconds: float = 3.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(credentials, SignerCredentialSlots):
            raise TypeError("signer_os_credential_slots_required")
        if type(account_alias) is not str or not account_alias:
            raise ValueError("binance_testnet_account_alias_required")
        if not 0.1 <= timeout_seconds <= 10:
            raise ValueError("invalid_binance_testnet_timeout")
        self.credentials = credentials
        self.account_alias = account_alias
        self.transport = transport or UrllibJsonTransport()
        self.timeout_seconds = timeout_seconds
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, object] | None = None,
        *,
        signed: bool,
        timestamp_ms: int | None = None,
    ):
        if method not in {"GET", "POST"} or not path.startswith("/fapi/"):
            raise ValueError("unsupported_binance_testnet_operation")
        values = dict(params or {})
        headers = {"Accept": "application/json"}
        if signed:
            key_id, secret = self.credentials.resolve(self.account_alias)
            values["timestamp"] = (
                timestamp_ms if timestamp_ms is not None else self.clock_ms()
            )
            values["recvWindow"] = 5000
            query = urlencode(values)
            signature = hmac.new(
                secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            url = f"{_TESTNET_BASE}{path}?{query}&signature={signature}"
            headers["X-MBX-APIKEY"] = key_id
        else:
            url = (
                f"{_TESTNET_BASE}{path}?{urlencode(values)}"
                if values
                else f"{_TESTNET_BASE}{path}"
            )
        try:
            result = self.transport.request(
                method, url, headers=headers, timeout=self.timeout_seconds
            )
        except BinancePaperExchangeError:
            raise
        except Exception:
            raise BinancePaperExchangeError(
                "binance_testnet_transport_unavailable"
            ) from None
        if not isinstance(result, (dict, list)):
            raise BinancePaperExchangeError("binance_testnet_response_invalid")
        return result

    def server_time_ms(self) -> int:
        result = self._request("GET", "/fapi/v1/time", signed=False)
        value = result.get("serverTime") if isinstance(result, dict) else None
        if type(value) is not int or value <= 0:
            raise BinancePaperExchangeError("binance_testnet_clock_invalid")
        return value

    def signed_request(
        self,
        method: str,
        path: str,
        params: Mapping[str, object] | None = None,
        *,
        timestamp_ms: int | None = None,
    ):
        return self._request(
            method, path, params, signed=True, timestamp_ms=timestamp_ms
        )


class BinanceUSDMPaperAccountEvidenceProvider:
    """Fetch trusted account, open-order, position and daily-loss evidence.

    This provider rejects hedge-mode positions and incomplete income enumeration.
    It is a Testnet adapter, not production account verification.
    """

    def __init__(
        self,
        *,
        client: BinanceUSDMPaperClient,
        max_open_orders: int = 5000,
        max_income_rows: int = 999,
    ) -> None:
        if not isinstance(client, BinanceUSDMPaperClient):
            raise TypeError("binance_testnet_client_required")
        if type(max_open_orders) is not int or not 1 <= max_open_orders <= 10_000:
            raise ValueError("invalid_open_order_bound")
        if type(max_income_rows) is not int or not 1 <= max_income_rows < 1000:
            raise ValueError("invalid_income_row_bound")
        self.client = client
        self.max_open_orders = max_open_orders
        self.max_income_rows = max_income_rows

    @staticmethod
    def _decimal(value: object, field: str) -> Decimal:
        if type(value) not in (str, int):
            raise BinancePaperExchangeError(f"binance_{field}_invalid")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise BinancePaperExchangeError(f"binance_{field}_invalid") from None
        if not result.is_finite():
            raise BinancePaperExchangeError(f"binance_{field}_invalid")
        return result

    def get_snapshot(self, account_alias: str, *, now_ms: int) -> AccountRiskSnapshot:
        if account_alias != self.client.account_alias:
            raise BinancePaperExchangeError("binance_account_alias_mismatch")
        request_started_ms = self.client.clock_ms()
        server_ms = self.client.server_time_ms()
        account = self.client.signed_request(
            "GET", "/fapi/v2/account", timestamp_ms=server_ms
        )
        open_orders = self.client.signed_request(
            "GET", "/fapi/v1/openOrders", timestamp_ms=server_ms
        )
        if (
            not isinstance(account, dict)
            or account.get("canTrade") is not True
            or not isinstance(account.get("assets"), list)
            or len(account["assets"]) > 1000
            or not isinstance(account.get("positions"), list)
            or not isinstance(open_orders, list)
            or len(open_orders) > self.max_open_orders
        ):
            raise BinancePaperExchangeError("binance_account_evidence_incomplete")

        seen_assets = set()
        for asset in account["assets"]:
            if not isinstance(asset, dict):
                raise BinancePaperExchangeError("binance_balance_row_invalid")
            code = asset.get("asset")
            if (
                type(code) is not str
                or not re.fullmatch(r"[A-Z0-9]{1,20}", code)
                or code in seen_assets
            ):
                raise BinancePaperExchangeError("binance_balance_asset_invalid")
            seen_assets.add(code)
            self._decimal(asset.get("walletBalance"), "wallet_balance")
            self._decimal(asset.get("availableBalance"), "available_balance")

        midnight = datetime.fromtimestamp(server_ms / 1000, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start_ms = int(midnight.timestamp() * 1000)
        income = self.client.signed_request(
            "GET",
            "/fapi/v1/income",
            {"startTime": start_ms, "endTime": server_ms, "limit": 1000},
            timestamp_ms=server_ms,
        )
        if not isinstance(income, list) or len(income) > self.max_income_rows:
            # A full 1000-row page is treated as incomplete; no risk decision is
            # made from a silently truncated daily PnL history.
            raise BinancePaperExchangeError("binance_daily_income_incomplete")

        positions: dict[str, Decimal] = {}
        symbol_exposure: dict[str, Decimal] = {}
        gross = Decimal("0")
        for row in account["positions"]:
            if not isinstance(row, dict):
                raise BinancePaperExchangeError("binance_position_row_invalid")
            symbol = row.get("symbol")
            if type(symbol) is not str or not re.fullmatch(r"[A-Z0-9]{2,30}", symbol):
                raise BinancePaperExchangeError("binance_position_symbol_invalid")
            if row.get("positionSide") != "BOTH":
                raise BinancePaperExchangeError("binance_hedge_mode_not_supported")
            quantity = self._decimal(row.get("positionAmt"), "position_quantity")
            notional = self._decimal(
                row.get("notional"), "position_notional"
            ).copy_abs()
            if symbol in positions:
                raise BinancePaperExchangeError("binance_duplicate_position_symbol")
            positions[symbol] = quantity
            gross += notional
            symbol_exposure[symbol] = (
                symbol_exposure.get(symbol, Decimal("0")) + notional
            )

        for row in open_orders:
            if not isinstance(row, dict):
                raise BinancePaperExchangeError("binance_open_order_invalid")
            symbol = row.get("symbol")
            if type(symbol) is not str or not symbol.isalnum():
                raise BinancePaperExchangeError("binance_open_order_symbol_invalid")
            if row.get("reduceOnly") is True or row.get("reduceOnly") == "true":
                continue
            if row.get("type") != "LIMIT":
                raise BinancePaperExchangeError("binance_open_order_type_unsupported")
            quantity = self._decimal(row.get("origQty"), "open_order_quantity")
            price = self._decimal(row.get("price"), "open_order_price")
            if quantity <= 0 or price <= 0:
                raise BinancePaperExchangeError("binance_open_order_notional_invalid")
            notional = quantity * price
            gross += notional
            symbol_exposure[symbol] = (
                symbol_exposure.get(symbol, Decimal("0")) + notional
            )

        net_income = Decimal("0")
        seen_receipts = set()
        for row in income:
            if not isinstance(row, dict):
                raise BinancePaperExchangeError("binance_income_row_invalid")
            kind = row.get("incomeType")
            receipt_time = row.get("time")
            receipt_id = row.get("tranId")
            if (
                type(kind) is not str
                or type(receipt_time) is not int
                or not start_ms <= receipt_time <= server_ms
                or type(receipt_id) not in (str, int)
                or not str(receipt_id).isdigit()
            ):
                raise BinancePaperExchangeError(
                    "binance_income_receipt_identity_invalid"
                )
            identity = (kind, str(receipt_id))
            if identity in seen_receipts:
                raise BinancePaperExchangeError("binance_duplicate_income_receipt")
            seen_receipts.add(identity)
            if kind in _INCOME_TYPES:
                net_income += self._decimal(row.get("income"), "income_amount")
        daily_loss = max(Decimal("0"), -net_income)

        request_finished_ms = self.client.clock_ms()
        if (
            type(now_ms) is not int
            or type(request_started_ms) is not int
            or type(request_finished_ms) is not int
            or request_started_ms < 0
            or request_finished_ms < request_started_ms
            or now_ms < 0
        ):
            raise BinancePaperExchangeError("binance_evidence_clock_invalid")
        # Account/position calls span a window. Age the evidence by the full
        # request duration rather than stamping it at the end and overstating
        # freshness. A slow or clock-skewed read therefore fails closed.
        request_duration_ms = request_finished_ms - request_started_ms
        captured_at_ms = min(now_ms - request_duration_ms, server_ms)
        if captured_at_ms < 0:
            raise BinancePaperExchangeError("binance_evidence_clock_invalid")
        return AccountRiskSnapshot(
            account_alias=account_alias,
            captured_at_ms=captured_at_ms,
            gross_exposure=gross,
            symbol_exposure=MappingProxyType(symbol_exposure),
            daily_loss=daily_loss,
            order_count_window=len(open_orders),
            positions=MappingProxyType(positions),
        )


class BinanceUSDMPaperExecutionAdapter:
    """Signer-owned LIMIT order dispatch to Futures Testnet only."""

    paper_only = True
    testnet_only = True

    def __init__(self, *, client: BinanceUSDMPaperClient) -> None:
        if not isinstance(client, BinanceUSDMPaperClient):
            raise TypeError("binance_testnet_client_required")
        self.client = client

    def execute(self, intent: ValidatedOrderIntent) -> PaperExecutionReceipt:
        if intent.order_type != "LIMIT" or intent.limit_price is None:
            return PaperExecutionReceipt("rejected", "testnet_limit_orders_only")
        try:
            timestamp = self.client.server_time_ms()
            result = self.client.signed_request(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": intent.symbol,
                    "side": intent.side,
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "quantity": format(intent.quantity, "f"),
                    "price": format(intent.limit_price, "f"),
                    "reduceOnly": "true" if intent.reduce_only else "false",
                    "newClientOrderId": intent.client_order_id,
                    "newOrderRespType": "RESULT",
                },
                timestamp_ms=timestamp,
            )
        except BinancePaperExchangeError:
            # A timeout/transport failure may happen after the exchange accepted
            # the order. Never report rejection or automatically resubmit.
            return PaperExecutionReceipt("unknown", "testnet_dispatch_ambiguous")
        if not isinstance(result, dict):
            return PaperExecutionReceipt("unknown", "testnet_response_unrecognized")
        if (
            result.get("clientOrderId") != intent.client_order_id
            or type(result.get("orderId")) is not int
        ):
            return PaperExecutionReceipt("unknown", "testnet_response_unrecognized")
        status = result.get("status")
        if status in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
            return PaperExecutionReceipt("accepted", "testnet_order_accepted")
        if status in {"REJECTED", "EXPIRED", "CANCELED"}:
            return PaperExecutionReceipt("rejected", "testnet_order_rejected")
        return PaperExecutionReceipt("unknown", "testnet_response_unrecognized")


    def reconcile(self, reservation: IntentReservation) -> PaperExecutionReceipt:
        """Read exchange order state by durable client ID; never resubmit an order."""
        if (
            not isinstance(reservation, IntentReservation)
            or not reservation.symbol
            or not reservation.client_order_id
        ):
            return PaperExecutionReceipt("unknown", "testnet_reconciliation_scope_missing")
        try:
            timestamp = self.client.server_time_ms()
            result = self.client.signed_request(
                "GET",
                "/fapi/v1/order",
                {
                    "symbol": reservation.symbol,
                    "origClientOrderId": reservation.client_order_id,
                },
                timestamp_ms=timestamp,
            )
        except BinancePaperExchangeError:
            return PaperExecutionReceipt("unknown", "testnet_reconciliation_unavailable")
        if (
            not isinstance(result, dict)
            or result.get("clientOrderId") != reservation.client_order_id
            or type(result.get("orderId")) is not int
        ):
            return PaperExecutionReceipt("unknown", "testnet_reconciliation_unrecognized")
        status = result.get("status")
        if status in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
            return PaperExecutionReceipt("accepted", "testnet_order_reconciled")
        if status in {"REJECTED", "EXPIRED", "CANCELED"}:
            try:
                executed = Decimal(str(result.get("executedQty", "0")))
            except InvalidOperation:
                return PaperExecutionReceipt("unknown", "testnet_reconciliation_unrecognized")
            if not executed.is_finite() or executed < 0:
                return PaperExecutionReceipt("unknown", "testnet_reconciliation_unrecognized")
            if executed > 0:
                return PaperExecutionReceipt("accepted", "testnet_order_partially_executed")
            return PaperExecutionReceipt("rejected", "testnet_order_not_executed")
        return PaperExecutionReceipt("unknown", "testnet_reconciliation_unrecognized")


class PaperSignerRecoveryWorker:
    """Reconcile durable ambiguous intents against Testnet without retrying them."""

    def __init__(
        self,
        *,
        replay_store: SQLiteReplayStore,
        adapter: BinanceUSDMPaperExecutionAdapter,
    ) -> None:
        if not isinstance(replay_store, SQLiteReplayStore):
            raise TypeError("durable_replay_store_required")
        if not isinstance(adapter, BinanceUSDMPaperExecutionAdapter):
            raise TypeError("binance_testnet_reconciliation_adapter_required")
        self.replay_store = replay_store
        self.adapter = adapter

    def run_once(self, *, now_ms: int, limit: int = 1000) -> dict[str, int]:
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("invalid_recovery_clock")
        resolved = 0
        unresolved = 0
        for reservation in self.replay_store.list_unresolved(limit=limit):
            receipt = self.adapter.reconcile(reservation)
            if receipt.status in {"accepted", "rejected"}:
                self.replay_store.resolve_reconciled(
                    reservation.intent_id,
                    status=receipt.status,
                    outcome_code=receipt.outcome_code,
                    now_ms=now_ms,
                )
                resolved += 1
            else:
                unresolved += 1
        return {"resolved": resolved, "unresolved": unresolved}
