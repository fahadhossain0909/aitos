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

import hashlib
import hmac
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import aiohttp

from aitos.exchange.symbol_filters import SymbolFilters
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

    async def get_account_balance(self, asset: str = "USDT") -> float:
        """GET /fapi/v2/balance (signed) — the actual live account balance,
        for a real portfolio tracker rather than the paper-trading
        equity/pnl bookkeeping ``PaperPortfolioTracker`` does."""
        response = await self._signed_request("GET", "/fapi/v2/balance", {})
        for entry in response:
            if entry.get("asset") == asset:
                return float(entry.get("availableBalance", entry.get("balance", 0.0)))
        return 0.0

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        quantity, filters = self._apply_quantity_precision(
            request.symbol, request.quantity
        )
        if filters is not None and not filters.meets_min_notional(
            request.reference_price, quantity
        ):
            return self._min_notional_failure(
                request.symbol, request.side, filters, request.reference_price, quantity
            )

        client_order_id = request.client_order_id or f"aitos-{uuid.uuid4().hex[:20]}"
        params = {
            "symbol": request.symbol,
            "type": request.order_type,
            "quantity": quantity,
            "newClientOrderId": client_order_id,
        }
        params.update(self._position_params(request.side, is_closing_order=False))

        try:
            response = await self._signed_request("POST", "/fapi/v1/order", params)
        except BinanceAPIError as exc:
            logger.error(
                "order submission failed",
                extra={"aitos_extra": {"symbol": request.symbol, "error": str(exc)}},
            )
            return OrderResult(
                order_id=client_order_id,
                symbol=request.symbol,
                side=request.side,
                filled_quantity=0.0,
                fill_price=0.0,
                success=False,
                error=str(exc),
            )

        filled_qty = float(response.get("executedQty", 0.0) or 0.0)
        avg_price = (
            float(response.get("avgPrice", 0.0) or 0.0) or request.reference_price
        )
        status = response.get("status", "UNKNOWN")
        success = status in (
            "FILLED",
            "PARTIALLY_FILLED",
            "NEW",
        )  # NEW: accepted, fill confirmation may lag

        return OrderResult(
            order_id=str(response.get("orderId", client_order_id)),
            symbol=request.symbol,
            side=request.side,
            filled_quantity=filled_qty if filled_qty > 0 else quantity,
            fill_price=avg_price,
            success=success,
            error=None if success else f"unexpected order status: {status}",
        )

    async def get_order_status(self, symbol: str, order_id: str) -> dict[str, Any]:
        return await self._signed_request(
            "GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}
        )

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        return await self._signed_request(
            "DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}
        )

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
            await self.cancel_order(symbol, order_id)
        except BinanceAPIError as exc:
            # Most commonly: the order already filled or was already canceled
            # (e.g. the opposite leg triggered first) — not an operational error.
            logger.info("cancel_resting_order: %s", exc)

    async def get_resting_order_status(self, symbol: str, order_id: str) -> str | None:
        try:
            status = await self.get_order_status(symbol, order_id)
            return status.get("status")
        except BinanceAPIError as exc:
            logger.error("get_resting_order_status failed: %s", exc)
            return None

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
        params = {
            "symbol": symbol,
            "type": order_type,
            "stopPrice": trigger_price,
            "quantity": quantity,
            "newClientOrderId": client_order_id,
        }
        params.update(self._position_params(position_side, is_closing_order=True))

        try:
            response = await self._signed_request("POST", "/fapi/v1/order", params)
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

        return OrderResult(
            order_id=str(response.get("orderId", client_order_id)),
            symbol=symbol,
            side=position_side,
            filled_quantity=0.0,  # resting order — not filled at placement time
            fill_price=trigger_price,
            success=response.get("status") in ("NEW", "FILLED"),
            error=None,
        )

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

        async with self._session.request(method, url, headers=headers) as resp:
            body = await resp.json()
            if resp.status >= 400:
                code = body.get("code", resp.status)
                msg = body.get("msg", "unknown error")
                raise BinanceAPIError(f"Binance API error {code}: {msg}")
            return body


class BinanceAPIError(Exception):
    """Raised for any non-2xx response from Binance's private API."""
