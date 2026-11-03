"""Windows MT5 gateway process.

Run this service on the Windows host that owns the MetaTrader 5 terminal.
It intentionally exposes a tiny API surface; strategy code never receives
broker credentials or talks to the terminal directly.
"""

from __future__ import annotations

import os
import secrets

from aiohttp import web

try:
    import MetaTrader5 as mt5
except ImportError as exc:  # pragma: no cover - only Windows gateway runtime
    raise RuntimeError(
        "install MetaTrader5 in the Windows gateway environment"
    ) from exc

TOKEN = os.environ.get("AITOS_MT5_GATEWAY_TOKEN")
if not TOKEN:
    raise RuntimeError("AITOS_MT5_GATEWAY_TOKEN must be configured")


def authorized(request: web.Request) -> bool:
    supplied = request.headers.get("Authorization", "")
    return secrets.compare_digest(supplied, f"Bearer {TOKEN}")


def ensure_mt5() -> None:
    if not mt5.initialize():
        raise web.HTTPServiceUnavailable(
            text=f"MT5 initialize failed: {mt5.last_error()}"
        )


def health(_: web.Request) -> web.Response:
    ensure_mt5()
    info = mt5.terminal_info()
    return web.json_response({"ok": info is not None, "terminal": str(info)})


def account(request: web.Request) -> web.Response:
    if not authorized(request):
        raise web.HTTPUnauthorized()
    ensure_mt5()
    info = mt5.account_info()
    if info is None:
        raise web.HTTPServiceUnavailable(text=str(mt5.last_error()))
    return web.json_response(
        {
            "login": info.login,
            "balance": info.balance,
            "equity": info.equity,
            "currency": info.currency,
            "margin": info.margin,
            "margin_free": info.margin_free,
        }
    )


def positions(request: web.Request) -> web.Response:
    if not authorized(request):
        raise web.HTTPUnauthorized()
    ensure_mt5()
    rows = mt5.positions_get() or ()
    return web.json_response(
        [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": p.type,
                "volume": p.volume,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "profit": p.profit,
            }
            for p in rows
        ]
    )


async def order(request: web.Request) -> web.Response:
    if not authorized(request):
        raise web.HTTPUnauthorized()
    ensure_mt5()
    body = await request.json()
    symbol = str(body["symbol"])
    quantity = float(body["quantity"])
    side = str(body["side"])
    order_type = str(body.get("order_type", "MARKET"))
    if not mt5.symbol_select(symbol, True):
        raise web.HTTPBadRequest(text=f"unknown MT5 symbol: {symbol}")
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise web.HTTPServiceUnavailable(text=f"no tick for {symbol}")
    if side == "LONG":
        mt5_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    elif side == "SHORT":
        mt5_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        raise web.HTTPBadRequest(text="side must be LONG or SHORT")
    if order_type != "MARKET":
        raise web.HTTPBadRequest(text="gateway currently permits MARKET only")

    request_data = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": quantity,
        "type": mt5_type,
        "price": price,
        "deviation": int(body.get("deviation", 20)),
        "magic": int(body.get("magic", 260906)),
        "comment": str(body.get("client_order_id") or "AITOS"),
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request_data)
    if result is None:
        raise web.HTTPServiceUnavailable(text=str(mt5.last_error()))
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise web.HTTPBadRequest(
            text=f"MT5 order rejected: {result.retcode} {result.comment}"
        )
    return web.json_response(
        {
            "order_id": str(result.order),
            "filled_quantity": float(result.volume),
            "fill_price": float(result.price),
            "success": True,
        }
    )


app = web.Application()
app.router.add_get("/health", health)
app.router.add_get("/v1/account", account)
app.router.add_get("/v1/positions", positions)
app.router.add_post("/v1/orders", order)

if __name__ == "__main__":
    web.run_app(
        app,
        host=os.environ.get("AITOS_MT5_GATEWAY_HOST", "0.0.0.0"),
        port=int(os.environ.get("AITOS_MT5_GATEWAY_PORT", "8787")),
    )
