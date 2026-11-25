"""Testable logic behind ``run_live_trading.py``. Split out from the
script itself so the confirmation gate and executor-preparation logic can
be unit tested without needing a real terminal or real Binance
credentials — see ``tests/test_live_trading.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from aitos.config.paths import get_model_data_dir
from aitos.exchange.binance import BinanceFuturesAdapter
from aitos.execution.binance_executor import BinanceFuturesOrderExecutor
from aitos.intelligence.capital_controls import ProbabilityCalibrator
from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.intelligence.capital_protection import CapitalReservation
from aitos.logging_setup import get_logger
from aitos.security.live_approval import LiveApprovalControlPlane

logger = get_logger("aitos.live_trading")

CONFIRMATION_PHRASE = "I APPROVE LIVE TRADING"


def create_live_approval_control_plane() -> LiveApprovalControlPlane:
    """Open the exact durable approval store used by the operator gateway.

    The gateway and executor must share AITOS_WEBAUTHN_DB, signing key,
    origin, RP ID and role map. Both derive the same sibling live-approvals.sqlite
    file; mismatches fail closed.
    """
    webauthn_db = os.environ.get("AITOS_WEBAUTHN_DB", "").strip()
    origin = os.environ.get("AITOS_WEBAUTHN_ORIGIN", "").strip()
    signing_key = os.environ.get("AITOS_APPROVAL_SIGNING_KEY", "")
    role_map_raw = os.environ.get("AITOS_OPERATOR_ROLE_MAP", "")
    if not all((webauthn_db, origin, signing_key, role_map_raw)):
        raise SystemExit(
            "mainnet approval requires shared AITOS_WEBAUTHN_DB, "
            "AITOS_WEBAUTHN_ORIGIN, AITOS_APPROVAL_SIGNING_KEY and "
            "AITOS_OPERATOR_ROLE_MAP"
        )
    parsed_origin = urlsplit(origin)
    if (
        parsed_origin.scheme != "https"
        or not parsed_origin.hostname
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        raise SystemExit("mainnet approval origin must be an exact HTTPS origin")
    try:
        role_map = json.loads(role_map_raw)
    except json.JSONDecodeError:
        raise SystemExit("AITOS_OPERATOR_ROLE_MAP must be valid JSON") from None
    if not isinstance(role_map, dict) or any(
        not isinstance(user, str)
        or not user.strip()
        or not isinstance(roles, list)
        or any(not isinstance(role, str) or not role for role in roles)
        for user, roles in role_map.items()
    ):
        raise SystemExit("AITOS_OPERATOR_ROLE_MAP must map users to role arrays")
    rp_id = os.environ.get("AITOS_WEBAUTHN_RP_ID", "").strip() or parsed_origin.hostname
    try:
        return LiveApprovalControlPlane(
            Path(webauthn_db).expanduser().resolve().with_name("live-approvals.sqlite"),
            signing_key=signing_key.encode("utf-8"),
            allowed_origins={origin},
            rp_id=rp_id,
            role_authorizer=lambda user, role: role in role_map.get(user, []),
        )
    except (TypeError, ValueError, OSError) as exc:
        raise SystemExit(
            "mainnet approval control plane configuration rejected: "
            f"{type(exc).__name__}"
        ) from None


def confirm_live_trading(
    symbols: list[str],
    testnet: bool,
    input_fn: Callable[[str], str] = input,
    *,
    approval_authority: LiveApprovalControlPlane | None = None,
) -> str:
    """Interactive, session-level human approval gate. Returns the
    operator's identifier to use as every opportunity's ``approved_by``
    for the run. Exits the process (via ``sys.exit(1)``) on any failed
    confirmation rather than returning a falsy value — a caller
    forgetting to check a return value must never be how this gate fails
    open.
    """
    if not testnet and (
        os.environ.get("AITOS_ENABLE_MAINNET_EXECUTION") != "1"
        or not os.environ.get("AITOS_EXECUTION_SCOPE", "").strip()
        or not isinstance(approval_authority, LiveApprovalControlPlane)
    ):
        logger.critical(
            "mainnet trading remains disabled unless the explicit environment gate "
            "and shared WebAuthn approval control plane are configured"
        )
        raise SystemExit(1)

    target = "TESTNET" if testnet else "MAINNET — REAL MONEY"
    print("=" * 70)
    print(f"AITOS is about to start LIVE trading against Binance {target}.")
    print(f"Symbols: {', '.join(symbols)}")
    print("Real orders will be placed on your account.")
    print("=" * 70)

    operator = input_fn(
        "Type your name/identifier to approve this session (or Ctrl-C to abort): "
    ).strip()
    if not operator:
        print("No identifier entered — aborting.")
        sys.exit(1)

    confirmation = input_fn(
        f"Type EXACTLY '{CONFIRMATION_PHRASE}' to proceed as '{operator}': "
    ).strip()
    if confirmation != CONFIRMATION_PHRASE:
        print("Confirmation text did not match — aborting.")
        sys.exit(1)

    return operator


async def prepare_live_executor(
    settings,
    symbols: list[str],
    *,
    approval_authority: LiveApprovalControlPlane | None = None,
) -> BinanceFuturesOrderExecutor:
    """Construct, connect, and fully prepare a live executor: verifies
    credentials exist, verifies the account's actual hedge-mode setting
    matches configuration (refusing to trade on a mismatched assumption
    rather than guessing), and loads real per-symbol precision from
    ``/fapi/v1/exchangeInfo`` before returning.
    """
    if not settings.binance.testnet and (
        os.environ.get("AITOS_ENABLE_MAINNET_EXECUTION") != "1"
        or not isinstance(approval_authority, LiveApprovalControlPlane)
    ):
        logger.critical(
            "mainnet executor construction requires the explicit environment gate "
            "and shared WebAuthn approval control plane"
        )
        raise SystemExit(1)

    api_key, api_secret = settings.binance.execution_credentials
    if not api_key or not api_secret:
        credential_names = (
            "BINANCE_FUTURES_TESTNET_API_KEY/BINANCE_FUTURES_TESTNET_API_SECRET"
            if settings.binance.testnet
            else "BINANCE_API_KEY/BINANCE_API_SECRET"
        )
        logger.error(
            "%s are not set for the selected Binance environment — cannot trade live",
            credential_names,
        )
        sys.exit(1)

    scope = hashlib.sha256(f"{settings.binance.testnet}:{api_key}".encode()).hexdigest()
    if not settings.binance.testnet and (
        os.environ.get("AITOS_EXECUTION_SCOPE", "").strip() != scope
    ):
        raise SystemExit(
            "AITOS_EXECUTION_SCOPE must match the selected Binance account"
        )
    # PostgreSQL is bound by run_live_trading.py before any strategy or
    # recovery component is initialized. The live executor must not create or
    # consult the legacy SQLite ExecutionIntentStore.
    executor = BinanceFuturesOrderExecutor(
        api_key=api_key,
        api_secret=api_secret,
        testnet=settings.binance.testnet,
        recv_window_ms=settings.binance.recv_window_ms,
        hedge_mode=settings.binance.hedge_mode,
        execution_scope=scope,
        intent_store=None,
        live_approval_authority=approval_authority,
        live_approval_token_provider=(
            approval_authority.get_unconsumed_token
            if approval_authority is not None
            else None
        ),
    )
    try:
        return await _prepare_connected_executor(executor, settings, symbols)
    except BaseException:
        try:
            await executor.close()
        except BaseException:
            logger.warning("live executor cleanup failed during preparation")
        raise


async def _prepare_connected_executor(executor, settings, symbols):
    await executor.connect()

    account_hedge_mode = await executor.get_position_mode()
    if account_hedge_mode != settings.binance.hedge_mode:
        logger.error(
            "BINANCE_HEDGE_MODE (%s) does not match the account's actual setting (%s) — refusing to trade with mismatched assumptions",
            settings.binance.hedge_mode,
            account_hedge_mode,
        )
        sys.exit(1)

    info_adapter = BinanceFuturesAdapter(
        environment="testnet" if settings.binance.testnet else "mainnet"
    )
    async with info_adapter:
        symbol_filters = await info_adapter.fetch_exchange_info(symbols=symbols)
    executor.load_symbol_filters(symbol_filters)
    logger.info(
        "loaded exchangeInfo precision",
        extra={"aitos_extra": {"symbols": list(symbol_filters.keys())}},
    )

    return executor


def create_live_capital_gateway(scope: str) -> CapitalGateway:
    """Stable single-runtime ownership; keep live and paper leases isolated.

    The canonical runner holds RuntimeOwnership for this model root. Never
    share this stable owner between concurrently running, unlocked workers.
    """
    if len(scope) != 64 or any(c not in "0123456789abcdef" for c in scope):
        raise ValueError("hashed execution scope required")
    root = get_model_data_dir()
    with ExitStack() as cleanup:
        reservation = CapitalReservation(
            root / f"capital_live_{scope}.sqlite3", owner_id=f"live:{scope}"
        )
        cleanup.callback(reservation.close)
        calibrator = ProbabilityCalibrator(
            path=root / "capital_probability_calibration.sqlite3"
        )
        cleanup.callback(calibrator.close)
        gateway = CapitalGateway(reservation=reservation, calibrator=calibrator)
        cleanup.pop_all()
        return gateway
