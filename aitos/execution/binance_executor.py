"""Live order execution against Binance USDT-M Futures — the real
counterpart to ``SimulatedOrderExecutor``.

Every call here places, queries, or cancels a REAL order once wired to
mainnet credentials. Per the AI Constitution, nothing in this class makes
that decision on its own: ``TradeLifecycle.submit_opportunity`` already
routes every ``is_production=True`` opportunity through
``AIKernel.enforce_governance`` first, and this executor defaults to
Binance's **testnet** unless ``testnet=False`` is passed explicitly and
deliberately.

Security notes:
- API key/secret are only ever read from ``BinanceSettings`` (env vars)
  or passed directly — never hardcoded, never logged. The signature
  itself is also never logged.
- Every signed request includes a ``recvWindow`` and server-relative
  ``timestamp`` per Binance's replay-protection scheme.
- ``client_order_id`` lets a caller retry a request idempotently —
  Binance rejects a duplicate `newClientOrderId` rather than double-filling.

Not implemented here (see the README's "Next steps" for why): per-symbol
precision now comes from real `/fapi/v1/exchangeInfo` data
(`aitos.exchange.symbol_filters`) via `load_symbol_filters`, exchange-side
stop/take-profit orders are implemented below
(`place_stop_loss_order`/`place_take_profit_order`), and hedge-mode
(dual-side position) is now supported via `hedge_mode=True` — Binance's
one-way mode remains the default since it's what most accounts use.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from numbers import Real
from typing import Any
from urllib.parse import urlencode

import aiohttp

from aitos.exchange.symbol_filters import SymbolFilters
from aitos.execution.intent_store import ExecutionIntentStore, ExecutionUncertainError
from aitos.execution.order_executor import OrderExecutor, OrderRequest, OrderResult
from aitos.logging_setup import get_logger
from aitos.models.trade import TradeSide

logger = get_logger("aitos.execution.binance")

MAINNET_URL = "https://fapi.binance.com"
TESTNET_URL = "https://testnet.binancefuture.com"


def round_step(value: float, precision: int) -> float:
    """Round a quantity/price down to ``precision`` decimal places. Kept for
    callers without full ``SymbolFilters`` data; prefer
    ``SymbolFilters.round_quantity``/``round_price`` when available, since
    Binance's actual step size isn't always a clean power of ten."""
    factor = 10**precision
    return int(value * factor) / factor


class BinanceFuturesOrderExecutor(OrderExecutor):
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        recv_window_ms: int = 5000,
        session_factory: Callable[[], aiohttp.ClientSession] = aiohttp.ClientSession,
        symbol_filters: dict[str, SymbolFilters] | None = None,
        hedge_mode: bool = False,
        intent_store: ExecutionIntentStore | None = None,
        conditional_orders_api: str = "algo",
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError(
                "api_key and api_secret are required for live order execution"
            )
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = TESTNET_URL if testnet else MAINNET_URL
        self._recv_window_ms = recv_window_ms
        self._session_factory = session_factory
        self._session: aiohttp.ClientSession | None = None
        self._symbol_filters: dict[str, SymbolFilters] = dict(symbol_filters or {})
        self._hedge_mode = hedge_mode
        if conditional_orders_api not in {"algo", "legacy"}:
            raise ValueError("conditional_orders_api must be algo or legacy")
        self._conditional_orders_api = conditional_orders_api
        self._intent_store = intent_store
        self._pending_execution: dict[str, str] = {}
        if not testnet:
            logger.warning(
                "BinanceFuturesOrderExecutor initialized against MAINNET — live orders will place real trades"
            )

    @property
    def hedge_mode(self) -> bool:
        return self._hedge_mode

    async def get_position_mode(self) -> bool:
        """GET /fapi/v1/positionSide/dual — the account's *actual* setting on
        Binance, independent of what this instance was constructed with.
        Useful to verify they agree before trading (a mismatch means every
        order below would be built with the wrong parameters)."""
        response = await self._signed_request("GET", "/fapi/v1/positionSide/dual", {})
        return bool(response.get("dualSidePosition", False))

    async def set_position_mode(self, hedge_mode: bool) -> None:
        """POST /fapi/v1/positionSide/dual — changes the account-wide
        setting on Binance itself. Only takes effect with no open positions
        or orders on the account. Also updates this instance's own
        ``hedge_mode`` flag so subsequent orders match."""
        await self._signed_request(
            "POST",
            "/fapi/v1/positionSide/dual",
            {"dualSidePosition": "true" if hedge_mode else "false"},
        )
        self._hedge_mode = hedge_mode

    def load_symbol_filters(self, filters: dict[str, SymbolFilters]) -> None:
        """Populate/refresh precision data — typically from
        ``exchange.fetch_exchange_info()`` on the data-layer adapter (public
        endpoint, shared with the rest of the system), called once at
        startup and periodically thereafter (Binance does change these)."""
        self._symbol_filters.update(filters)

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = self._session_factory()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return await self._signed_request(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}
        )

    async def get_position_risk(self) -> list[dict[str, Any]]:
        """Read-only full-account snapshot; no symbol filter can hide exposure."""
        return await self._signed_request("GET", "/fapi/v3/positionRisk", {})

    async def get_open_algo_orders(
        self, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        """Read-only authenticated inventory of currently open conditional orders.

        This is inventory evidence only: it does not reconcile child fills,
        positions, or the local intent journal and never authorizes recovery.
        An incomplete or malformed response is rejected as a whole.
        """
        params: dict[str, Any] = {"algoType": "CONDITIONAL"}
        if symbol is not None:
            normalized_symbol = str(symbol).strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{1,30}", normalized_symbol):
                raise ValueError("invalid symbol for algo inventory")
            params["symbol"] = normalized_symbol
        response = await self._signed_request("GET", "/fapi/v1/openAlgoOrders", params)
        if not isinstance(response, list):
            raise ValueError("algo inventory must be a complete list")
        seen_ids: set[str] = set()
        seen_clients: set[str] = set()
        validated: list[dict[str, Any]] = []
        supported_types = {
            "STOP_MARKET",
            "TAKE_PROFIT_MARKET",
            "STOP",
            "TAKE_PROFIT",
            "TRAILING_STOP_MARKET",
        }
        for row in response:
            if not isinstance(row, dict):
                raise ValueError("malformed algo inventory row")
            algo_id = row.get("algoId")
            client_id = row.get("clientAlgoId")
            row_symbol = row.get("symbol")
            if (
                isinstance(algo_id, bool)
                or not str(algo_id or "").isdigit()
                or int(algo_id) <= 0
                or not isinstance(client_id, str)
                or not client_id.strip()
                or not isinstance(row_symbol, str)
                or not re.fullmatch(r"[A-Z0-9]{1,30}", row_symbol)
                or (symbol is not None and row_symbol != params["symbol"])
                or row.get("algoType") != "CONDITIONAL"
                or row.get("algoStatus") != "NEW"
                or row.get("orderType") not in supported_types
                or row.get("side") not in {"BUY", "SELL"}
                or row.get("positionSide") not in {"BOTH", "LONG", "SHORT"}
            ):
                raise ValueError("invalid or non-open algo inventory identity")
            quantity = row.get("quantity")
            if isinstance(quantity, bool):
                raise ValueError("invalid algo inventory quantity")
            try:
                quantity_value = float(quantity)
            except (TypeError, ValueError):
                quantity_value = float("nan")
            close_position = row.get("closePosition")
            reduce_only = row.get("reduceOnly")
            if not (type(close_position) is bool and type(reduce_only) is bool):
                raise ValueError("invalid algo inventory protection flags")
            if not close_position and (
                not math.isfinite(quantity_value) or quantity_value <= 0
            ):
                raise ValueError("invalid algo inventory quantity")
            if row.get("orderType") == "TRAILING_STOP_MARKET":
                trigger = row.get("activatePrice") or row.get("triggerPrice")
            else:
                trigger = row.get("triggerPrice")
            if isinstance(trigger, bool):
                raise ValueError("invalid algo inventory trigger")
            try:
                trigger_value = float(trigger)
            except (TypeError, ValueError):
                trigger_value = float("nan")
            if not math.isfinite(trigger_value) or trigger_value <= 0:
                raise ValueError("invalid algo inventory trigger")
            identity = str(int(algo_id))
            client_identity = client_id.strip()
            if identity in seen_ids or client_identity in seen_clients:
                raise ValueError("duplicate algo inventory identity")
            seen_ids.add(identity)
            seen_clients.add(client_identity)
            validated.append(dict(row))
        return validated

    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Read-only inventory of ordinary open USD-M orders.

        Conditional algo orders are returned by ``get_open_algo_orders`` and
        must be reconciled separately. Malformed, duplicate or cross-symbol
        rows invalidate the entire snapshot rather than silently dropping risk.
        """
        params: dict[str, Any] = {}
        if symbol is not None:
            normalized_symbol = str(symbol).strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{1,30}", normalized_symbol):
                raise ValueError("invalid symbol for open-order inventory")
            params["symbol"] = normalized_symbol
        response = await self._signed_request("GET", "/fapi/v1/openOrders", params)
        if not isinstance(response, list):
            raise ValueError("open-order inventory must be a complete list")
        seen_ids: set[str] = set()
        validated: list[dict[str, Any]] = []
        supported_types = {
            "LIMIT",
            "MARKET",
            "STOP",
            "STOP_MARKET",
            "TAKE_PROFIT",
            "TAKE_PROFIT_MARKET",
            "TRAILING_STOP_MARKET",
        }
        for row in response:
            if not isinstance(row, dict):
                raise ValueError("malformed open-order inventory row")
            order_id = row.get("orderId")
            row_symbol = row.get("symbol")
            if (
                isinstance(order_id, bool)
                or not str(order_id or "").isdigit()
                or int(order_id) <= 0
                or not isinstance(row_symbol, str)
                or not re.fullmatch(r"[A-Z0-9]{1,30}", row_symbol)
                or (symbol is not None and row_symbol != params["symbol"])
                or row.get("status") not in {"NEW", "PARTIALLY_FILLED"}
                or row.get("side") not in {"BUY", "SELL"}
                or row.get("positionSide") not in {"BOTH", "LONG", "SHORT"}
                or row.get("type") not in supported_types
            ):
                raise ValueError("invalid or non-open order inventory identity")
            identity = str(int(order_id))
            if identity in seen_ids:
                raise ValueError("duplicate open-order identity")
            seen_ids.add(identity)
            for field_name in ("origQty", "executedQty"):
                raw = row.get(field_name)
                if isinstance(raw, bool):
                    raise ValueError("invalid open-order quantity")
                try:
                    number = float(raw)
                except (TypeError, ValueError):
                    number = float("nan")
                if not math.isfinite(number) or number < 0:
                    raise ValueError("invalid open-order quantity")
                if field_name == "origQty" and number <= 0:
                    raise ValueError("invalid original order quantity")
            if float(row["executedQty"]) > float(row["origQty"]):
                raise ValueError("executed quantity exceeds original quantity")
            for flag in ("reduceOnly", "closePosition"):
                value = row.get(flag)
                if not (
                    type(value) is bool
                    or (type(value) is str and value in {"true", "false"})
                ):
                    raise ValueError(f"invalid open-order {flag} evidence")
            validated.append(dict(row))
        return validated

    async def get_account_balance(self, asset: str = "USDT") -> float:
        """GET /fapi/v2/balance (signed) — the actual live account balance,
        for a real portfolio tracker rather than the paper-trading
        equity/pnl bookkeeping ``PaperPortfolioTracker`` does."""
        response = await self._signed_request("GET", "/fapi/v2/balance", {})
        for entry in response:
            if entry.get("asset") == asset:
                return float(entry.get("availableBalance", entry.get("balance", 0.0)))
        return 0.0

    @property
    def execution_scope(self) -> str:
        if self._intent_store is None:
            raise RuntimeError("durable execution scope is not configured")
        return self._intent_store.scope

    @property
    def execution_intent_store(self) -> ExecutionIntentStore | None:
        """Expose the scoped durable intent journal for read-only diagnostics."""
        return self._intent_store

    @property
    def requires_exchange_execution(self) -> bool:
        return True

    def has_unresolved_orders(self, symbol: str | None = None) -> bool:
        return any(
            symbol is None or candidate == symbol
            for candidate in self._pending_execution.values()
        ) or bool(self._intent_store and self._intent_store.unresolved(symbol))

    def acknowledge_execution(self, client_order_id: str, trade_id: str) -> None:
        if self._intent_store is not None:
            self._intent_store.mark_applied(client_order_id, trade_id)
        self._pending_execution.pop(client_order_id, None)

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        for name in ("quantity", "reference_price"):
            raw = getattr(request, name)
            if isinstance(raw, bool) or not isinstance(raw, Real):
                raise ValueError(f"{name} must be finite and positive")
            if not math.isfinite(raw) or raw <= 0:
                raise ValueError(f"{name} must be finite and positive")
        quantity, filters = self._apply_quantity_precision(
            request.symbol, request.quantity
        )
        if filters is not None and not filters.meets_min_notional(
            request.reference_price, quantity
        ):
            return self._min_notional_failure(
                request.symbol, request.side, filters, request.reference_price, quantity
            )
        if quantity <= 0:
            return OrderResult(
                "unsubmitted",
                request.symbol,
                request.side,
                0,
                0,
                success=False,
                error="quantity rounds to zero",
            )
        client_id = request.client_order_id or f"aitos-{uuid.uuid4().hex[:20]}"
        request = replace(request, client_order_id=client_id)
        params = {
            "symbol": request.symbol,
            "type": request.order_type,
            "quantity": quantity,
            "newClientOrderId": client_id,
            "newOrderRespType": "RESULT",
        }
        params.update(
            self._position_params(request.side, is_closing_order=request.reduce_only)
        )
        if self._intent_store is not None:
            self._intent_store.begin(request)
        self._pending_execution[client_id] = request.symbol
        try:
            try:
                response = await self._signed_request("POST", "/fapi/v1/order", params)
            except BinanceAPIError as exc:
                # Only explicit known no-acceptance errors release capital. A
                # timeout/server/duplicate error can coexist with a real fill.
                if exc.code in {-2019, -1013, -1111, -1100, -1102, -1121, -2022}:
                    result = OrderResult(
                        client_id,
                        request.symbol,
                        request.side,
                        0,
                        0,
                        success=False,
                        error=str(exc),
                    )
                    return self._record_execution(client_id, result)
                response = await self.get_order_by_client_id(request.symbol, client_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                response = await self.get_order_by_client_id(request.symbol, client_id)
            if not isinstance(response, dict):
                raise ValueError("no authoritative order evidence")
            if response.get("status") in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}:
                response = await self.get_order_by_client_id(request.symbol, client_id)
                if isinstance(response, dict) and response.get("status") in {
                    "NEW",
                    "PARTIALLY_FILLED",
                    "PENDING_NEW",
                }:
                    # Bounded settlement, not background/indefinite polling.
                    # Cancellation ACK alone is not proof of zero execution.
                    await self._signed_request(
                        "DELETE",
                        "/fapi/v1/order",
                        {
                            "symbol": request.symbol,
                            "origClientOrderId": client_id,
                        },
                    )
                    response = await self.get_order_by_client_id(
                        request.symbol, client_id
                    )
            result = self._confirmed_result(request, quantity, response, params["side"])
            return self._record_execution(client_id, result)
        except asyncio.CancelledError:
            # The committed SUBMITTING intent survives cancellation/restart.
            raise
        except Exception:
            raise ExecutionUncertainError(
                request.symbol,
                client_id,
                "authoritative terminal fill/rejection not established",
            ) from None

    def _record_execution(self, client_id: str, result: OrderResult) -> OrderResult:
        if self._intent_store is not None:
            self._intent_store.record_result(client_id, result)
        if not result.success:
            self._pending_execution.pop(client_id, None)
        return result

    @staticmethod
    def _confirmed_result(
        request: OrderRequest, quantity: float, response: Any, side: str
    ) -> OrderResult:
        if not isinstance(response, dict):
            raise ValueError("missing authoritative order evidence")
        for field, expected in (
            ("symbol", request.symbol),
            ("side", side),
            ("clientOrderId", request.client_order_id),
        ):
            if field in response and response[field] != expected:
                raise ValueError("order evidence identity mismatch")
        status = response.get("status")
        if status not in {
            "FILLED",
            "CANCELED",
            "EXPIRED",
            "EXPIRED_IN_MATCH",
            "REJECTED",
        }:
            raise ValueError("order is not terminal")
        if "executedQty" not in response or isinstance(response["executedQty"], bool):
            raise ValueError("missing or boolean execution quantity")
        filled = float(response["executedQty"])
        if not math.isfinite(filled) or filled < 0 or filled > quantity * (1 + 1e-9):
            raise ValueError("invalid executed quantity")
        if status == "FILLED" and not math.isclose(
            filled, quantity, rel_tol=1e-9, abs_tol=1e-12
        ):
            raise ValueError("FILLED evidence does not cover the submitted quantity")
        if filled == 0:
            if status == "FILLED":
                raise ValueError("FILLED evidence has no execution")
            return OrderResult(
                str(response.get("orderId", request.client_order_id)),
                request.symbol,
                request.side,
                0,
                0,
                success=False,
                error=f"order was not filled (terminal status: {status})",
            )
        if isinstance(response.get("avgPrice"), bool) or isinstance(
            response.get("orderId"), bool
        ):
            raise ValueError("boolean execution price/identity")
        price = float(response.get("avgPrice", 0) or 0)
        if price == 0 and response.get("cumQuote") is not None:
            price = float(response["cumQuote"]) / filled
        if not math.isfinite(price) or price <= 0 or not response.get("orderId"):
            raise ValueError("invalid executed price/order identity")
        return OrderResult(
            str(response["orderId"]), request.symbol, request.side, filled, price
        )

    async def get_order_status(self, symbol: str, order_id: str) -> dict[str, Any]:
        if order_id.startswith("algo:"):
            algo_id = order_id.removeprefix("algo:")
            if not algo_id.isdigit() or int(algo_id) <= 0:
                raise ValueError("invalid algo order identity")
            evidence = await self._signed_request(
                "GET", "/fapi/v1/algoOrder", {"algoId": algo_id}
            )
            if (
                not isinstance(evidence, dict)
                or str(evidence.get("algoId")) != algo_id
                or ("symbol" in evidence and evidence["symbol"] != symbol)
            ):
                raise ExecutionUncertainError(
                    symbol, order_id, "algo evidence identity mismatch"
                )
            child_id = str(evidence.get("actualOrderId") or "")
            if child_id and child_id != "0":
                child = await self._signed_request(
                    "GET",
                    "/fapi/v1/order",
                    {
                        "symbol": symbol,
                        "orderId": child_id,
                    },
                )
                if (
                    not isinstance(child, dict)
                    or str(child.get("orderId")) != child_id
                    or ("symbol" in child and child["symbol"] != symbol)
                ):
                    raise ExecutionUncertainError(
                        symbol, order_id, "algo child identity mismatch"
                    )
                return {**child, "sourceAlgoId": order_id}
            status = evidence.get("algoStatus")
            if status not in {"NEW", "CANCELED", "EXPIRED", "REJECTED"}:
                raise ExecutionUncertainError(
                    symbol, order_id, "triggered algo has no confirmed child execution"
                )
            return {
                "orderId": order_id,
                "symbol": symbol,
                "status": status,
                "executedQty": "0",
                "avgPrice": "0",
                "sourceAlgoId": order_id,
            }
        return await self._signed_request(
            "GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}
        )

    async def get_order_by_client_id(
        self, symbol: str, client_order_id: str
    ) -> dict[str, Any] | None:
        try:
            return await self._signed_request(
                "GET",
                "/fapi/v1/order",
                {"symbol": symbol, "origClientOrderId": client_order_id},
            )
        except BinanceAPIError as exc:
            if exc.code == -2013:
                return None
            raise

    async def get_algo_order_by_client_id(
        self, symbol: str, client_algo_id: str
    ) -> dict[str, Any] | None:
        """Read-only lookup for protective POST-to-ACK crash recovery.

        Binance accepts either algoId or clientAlgoId for this USER_DATA query.
        Only the explicit venue not-found response returns None; transport and
        other API failures remain errors and must not release local quarantine.
        """
        normalized_symbol = str(symbol).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{1,30}", normalized_symbol):
            raise ValueError("invalid symbol for algo client-id lookup")
        if not isinstance(client_algo_id, str) or not client_algo_id.strip():
            raise ValueError("client algo identity is required")
        try:
            response = await self._signed_request(
                "GET",
                "/fapi/v1/algoOrder",
                {"clientAlgoId": client_algo_id},
            )
        except BinanceAPIError as exc:
            if exc.code == -2013:
                return None
            raise
        if not isinstance(response, dict):
            raise ExecutionUncertainError(
                normalized_symbol,
                client_algo_id,
                "algo client-id lookup returned malformed evidence",
            )
        algo_id = response.get("algoId")
        if (
            isinstance(algo_id, bool)
            or not str(algo_id or "").isdigit()
            or int(algo_id) <= 0
            or response.get("clientAlgoId") != client_algo_id
            or response.get("symbol") != normalized_symbol
            or response.get("algoType") != "CONDITIONAL"
            or response.get("orderType")
            not in {
                "STOP_MARKET",
                "TAKE_PROFIT_MARKET",
                "STOP",
                "TAKE_PROFIT",
                "TRAILING_STOP_MARKET",
            }
            or response.get("side") not in {"BUY", "SELL"}
            or response.get("positionSide") not in {"BOTH", "LONG", "SHORT"}
            or response.get("algoStatus")
            not in {"NEW", "CANCELED", "EXPIRED", "REJECTED", "TRIGGERED", "FINISHED"}
        ):
            raise ExecutionUncertainError(
                normalized_symbol,
                client_algo_id,
                "algo client-id lookup identity mismatch",
            )
        return dict(response)

    def _record_terminal_protection(
        self, symbol: str, order_id: str, evidence: dict[str, Any]
    ) -> None:
        if self._intent_store is None or evidence.get("status") not in {
            "CANCELED",
            "FILLED",
            "EXPIRED",
            "EXPIRED_IN_MATCH",
            "REJECTED",
        }:
            return
        raw_quantity = evidence.get("executedQty", 0)
        if isinstance(raw_quantity, bool):
            raise ExecutionUncertainError(
                symbol, order_id, "terminal protective fill quantity is invalid"
            )
        try:
            executed_quantity = float(raw_quantity)
        except (TypeError, ValueError, OverflowError):
            raise ExecutionUncertainError(
                symbol, order_id, "terminal protective fill quantity is invalid"
            ) from None
        try:
            self._intent_store.mark_protection_terminal(
                order_id,
                symbol=symbol,
                status=str(evidence["status"]),
                executed_quantity=executed_quantity,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ExecutionUncertainError(
                symbol, order_id, "terminal protective journal update failed"
            ) from exc

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        if order_id.startswith("algo:"):
            algo_id = order_id.removeprefix("algo:")
            if not algo_id.isdigit() or int(algo_id) <= 0:
                raise ValueError("invalid algo order identity")
            await self._signed_request(
                "DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}
            )
            evidence = await self.get_order_status(symbol, order_id)
        else:
            evidence = await self._signed_request(
                "DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}
            )
        if isinstance(evidence, dict):
            self._record_terminal_protection(symbol, order_id, evidence)
        return evidence

    @property
    def supports_exchange_side_stops(self) -> bool:
        return True

    async def place_stop_loss_order(
        self, symbol: str, side: TradeSide, quantity: float, stop_price: float
    ) -> OrderResult:
        return await self._place_reduce_only_stop(
            symbol, side, quantity, stop_price, order_type="STOP_MARKET", label="sl"
        )

    async def place_take_profit_order(
        self, symbol: str, side: TradeSide, quantity: float, take_profit_price: float
    ) -> OrderResult:
        return await self._place_reduce_only_stop(
            symbol,
            side,
            quantity,
            take_profit_price,
            order_type="TAKE_PROFIT_MARKET",
            label="tp",
        )

    async def cancel_resting_order(self, symbol: str, order_id: str) -> None:
        try:
            evidence = await self.cancel_order(symbol, order_id)
        except BinanceAPIError as exc:
            if exc.code not in {-2011, -2013}:
                raise
            evidence = await self.get_order_status(symbol, order_id)
        if isinstance(evidence, dict) and (
            (
                "orderId" in evidence
                and str(evidence["orderId"]) != str(order_id)
                and evidence.get("sourceAlgoId") != order_id
            )
            or ("symbol" in evidence and evidence["symbol"] != symbol)
        ):
            raise ExecutionUncertainError(
                symbol, order_id, "resting cancellation identity mismatch"
            )
        if not isinstance(evidence, dict) or evidence.get("status") not in {
            "CANCELED",
            "FILLED",
            "EXPIRED",
            "EXPIRED_IN_MATCH",
            "REJECTED",
        }:
            raise ExecutionUncertainError(
                symbol, order_id, "resting cancellation not confirmed"
            )

    async def get_resting_order_status(self, symbol: str, order_id: str) -> str | None:
        evidence = await self.get_order_status(symbol, order_id)
        if not isinstance(evidence, dict) or not evidence.get("status"):
            raise ExecutionUncertainError(
                symbol, order_id, "resting status unavailable"
            )
        status = str(evidence["status"])
        if status in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}:
            quantity = float(evidence.get("executedQty", 0))
            if not math.isfinite(quantity) or quantity < 0:
                raise ExecutionUncertainError(
                    symbol, order_id, "invalid resting execution quantity"
                )
            if quantity > 0:
                return "PARTIALLY_FILLED_TERMINAL"
        return status

    async def _place_reduce_only_stop(
        self,
        symbol: str,
        position_side: TradeSide,
        quantity: float,
        trigger_price: float,
        order_type: str,
        label: str,
    ) -> OrderResult:
        """STOP_MARKET / TAKE_PROFIT_MARKET orders that only ever reduce the
        existing position. In one-way mode that's via ``reduceOnly``; in
        hedge mode Binance infers "reduce" from ``positionSide`` alone and
        actually rejects orders that send both, so the two modes build
        different (but equivalent-intent) parameter sets — see
        ``_position_params``."""
        quantity, filters = self._apply_quantity_precision(symbol, quantity)
        if filters is not None:
            trigger_price = filters.round_price(trigger_price)
            if not filters.meets_min_notional(trigger_price, quantity):
                return self._min_notional_failure(
                    symbol, position_side, filters, trigger_price, quantity
                )

        client_order_id = f"aitos-{label}-{uuid.uuid4().hex[:16]}"
        if self._intent_store is not None:
            for pending in self._intent_store.unresolved(symbol):
                if json.loads(pending["request_json"])["order_type"] in {
                    "STOP_MARKET",
                    "TAKE_PROFIT_MARKET",
                }:
                    raise ExecutionUncertainError(
                        symbol,
                        pending["client_id"],
                        "prior protective intent requires reconciliation",
                    )
            self._intent_store.begin(
                OrderRequest(
                    symbol=symbol,
                    side=position_side,
                    quantity=quantity,
                    reference_price=trigger_price,
                    order_type=order_type,
                    client_order_id=client_order_id,
                    reduce_only=True,
                )
            )
        self._pending_execution[client_order_id] = symbol
        params = {
            "symbol": symbol,
            "type": order_type,
            "stopPrice": trigger_price,
            "quantity": quantity,
            "newClientOrderId": client_order_id,
        }
        params.update(self._position_params(position_side, is_closing_order=True))

        endpoint = "/fapi/v1/order"
        if self._conditional_orders_api == "algo":
            endpoint = "/fapi/v1/algoOrder"
            params["algoType"] = "CONDITIONAL"
            params["triggerPrice"] = params.pop("stopPrice")
            params["clientAlgoId"] = params.pop("newClientOrderId")
        try:
            response = await self._signed_request("POST", endpoint, params)
        except BinanceAPIError as exc:
            logger.error(
                f"{label} order placement failed",
                extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
            )
            return OrderResult(
                order_id=client_order_id,
                symbol=symbol,
                side=position_side,
                filled_quantity=0.0,
                fill_price=0.0,
                success=False,
                error=str(exc),
            )

        if self._conditional_orders_api == "algo":
            if not response.get("algoId") or response.get("algoStatus") != "NEW":
                raise ExecutionUncertainError(
                    symbol,
                    client_order_id,
                    "algo protection acceptance not established",
                )
            result = OrderResult(
                order_id=f"algo:{response['algoId']}",
                symbol=symbol,
                side=position_side,
                filled_quantity=0,
                fill_price=trigger_price,
                client_order_id=client_order_id,
            )
        else:
            if not response.get("orderId") or response.get("status") != "NEW":
                raise ExecutionUncertainError(
                    symbol,
                    client_order_id,
                    "legacy protection acceptance not established",
                )
            result = OrderResult(
                order_id=str(response["orderId"]),
                symbol=symbol,
                side=position_side,
                filled_quantity=0,
                fill_price=trigger_price,
                success=True,
                client_order_id=client_order_id,
            )
        if result.success and self._intent_store is not None:
            self._intent_store.record_protection(client_order_id, result)
        return result

    # -- Internals --------------------------------------------------------------

    def _position_params(
        self, position_side: TradeSide, is_closing_order: bool
    ) -> dict[str, Any]:
        """Build the ``side`` (+ ``positionSide``/``reduceOnly`` as needed)
        parameters for either mode:

        - **One-way mode**: a single ``side`` (BUY/SELL) says everything —
          opening a LONG or closing a SHORT are both ``BUY``. Closing
          orders add ``reduceOnly=true`` so they can never flip/add to the
          position.
        - **Hedge mode**: ``positionSide`` (LONG/SHORT) says *which* of the
          two simultaneous positions this order affects; ``side`` alone
          would be ambiguous (a BUY could open a LONG or close a SHORT).
          Binance rejects ``reduceOnly`` together with ``positionSide``, so
          it's omitted — the position side itself makes the intent
          unambiguous.
        """
        is_long = position_side == TradeSide.LONG

        if self._hedge_mode:
            side = (
                ("SELL" if is_long else "BUY")
                if is_closing_order
                else ("BUY" if is_long else "SELL")
            )
            return {"side": side, "positionSide": "LONG" if is_long else "SHORT"}

        side = (
            ("SELL" if is_long else "BUY")
            if is_closing_order
            else ("BUY" if is_long else "SELL")
        )
        params: dict[str, Any] = {"side": side}
        if is_closing_order:
            params["reduceOnly"] = "true"
        return params

    def _apply_quantity_precision(
        self, symbol: str, quantity: float
    ) -> tuple[float, SymbolFilters | None]:
        filters = self._symbol_filters.get(symbol)
        if filters is None:
            return quantity, None
        return filters.round_quantity(quantity), filters

    def _min_notional_failure(
        self,
        symbol: str,
        side: TradeSide,
        filters: SymbolFilters,
        price: float,
        quantity: float,
    ) -> OrderResult:
        error = (
            f"order notional {price * quantity:.4f} is below {symbol}'s minimum notional "
            f"{filters.min_notional:.4f} — rejected locally without hitting the API"
        )
        logger.warning(
            "order below min notional",
            extra={"aitos_extra": {"symbol": symbol, "notional": price * quantity}},
        )
        return OrderResult(
            order_id="",
            symbol=symbol,
            side=side,
            filled_quantity=0.0,
            fill_price=0.0,
            success=False,
            error=error,
        )

    def _sign(self, params: dict[str, Any]) -> str:
        query_string = urlencode(params)
        signature = hmac.new(
            self._api_secret.encode(), query_string.encode(), hashlib.sha256
        ).hexdigest()
        return f"{query_string}&signature={signature}"

    async def _signed_request(
        self, method: str, path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError(
                "BinanceFuturesOrderExecutor.connect() must be called first"
            )

        full_params = dict(params)
        full_params["timestamp"] = int(time.time() * 1000)
        full_params["recvWindow"] = self._recv_window_ms
        signed_query = self._sign(full_params)

        url = f"{self._base_url}{path}?{signed_query}"
        headers = {"X-MBX-APIKEY": self._api_key}

        try:
            async with self._session.request(method, url, headers=headers) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    code = body.get("code", resp.status)
                    msg = body.get("msg", "unknown error")
                    raise BinanceAPIError(f"Binance API error {code}: {msg}", code=code)
                return body
        except asyncio.CancelledError:
            raise
        except BinanceAPIError:
            raise
        except Exception:
            # aiohttp exceptions can embed the signed URL. Never forward its
            # signature/header context into lifecycle telemetry or error logs.
            raise BinanceTransportError("private exchange transport failed") from None


class BinanceTransportError(RuntimeError):
    """Sanitized private-request failure; submission outcome may be unknown."""


class BinanceAPIError(Exception):
    """Structured venue error; absent/unknown codes are not proof of rejection."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        match = re.search(r"error (-?\d+):", message)
        self.code = (
            int(code) if code is not None else (int(match[1]) if match else None)
        )
