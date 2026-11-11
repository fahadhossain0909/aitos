"""Command-line bootstrap for a user-controlled paper signer host.

Run as the dedicated local user on the trusted signer device, never on a
provider-managed VPS/RDP. Exchange secrets are entered interactively and stored
only through the platform credential vault.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aitos.exchange.binance_usdm_paper_signer import (
    BinanceUSDMPaperAccountEvidenceProvider,
    BinanceUSDMPaperClient,
    BinanceUSDMPaperExecutionAdapter,
    PaperSignerRecoveryWorker,
)
from aitos.native.credential_vault import get_credential_vault
from aitos.native.credential_profiles import (
    CredentialProfileStore,
    CredentialProfileError,
    CredentialProviderRouter,
    LazyNativeCredentialStore,
    discover_external_vault_backends,
)
from aitos.security.mtls import build_server_context
from aitos.security.paper_processor import PaperOnlyIntentProcessor
from aitos.security.replay_store import SQLiteReplayStore
from aitos.security.risk_limits import SignerRiskLimits
from aitos.security.risk_state import SQLiteSignerRiskState
from aitos.security.signer_credentials import SignerCredentialSlots
from aitos.security.signer_service import (
    PaperSignerRPCServer,
    SQLiteSignerRateLimiter,
    SQLiteSignerRegistry,
)
from aitos.security.signing_policy import OrderIntentPolicy


def _data_dir() -> Path:
    default = Path.home() / ".local" / "share" / "aitos-signer"
    path = Path(os.environ.get("AITOS_SIGNER_DATA_DIR", default)).expanduser()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise RuntimeError("signer_data_directory_must_be_private_mode_0700")
    return path


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required_signer_setting_missing:{name}")
    return value


def _decimal_env(name: str) -> Decimal:
    try:
        value = Decimal(_required(name))
    except (InvalidOperation, ValueError):
        raise RuntimeError(f"invalid_signer_decimal_setting:{name}") from None
    if not value.is_finite() or value <= 0:
        raise RuntimeError(f"invalid_signer_decimal_setting:{name}")
    return value


def _symbols() -> frozenset[str]:
    values = frozenset(
        part.strip().upper()
        for part in _required("AITOS_SIGNER_ALLOWED_SYMBOLS").split(",")
        if part.strip()
    )
    if not values:
        raise RuntimeError("signer_symbol_allowlist_required")
    return values


def _profiles() -> CredentialProfileStore:
    return CredentialProfileStore(_data_dir() / "credential-profiles.sqlite3")


def _slots() -> SignerCredentialSlots:
    profiles = _profiles()
    try:
        account = _required("AITOS_SIGNER_ACCOUNT_ALIAS")
        profile = profiles.resolve(account)
        backends = (
            discover_external_vault_backends()
            if profile.mode == "external"
            else {}
        )
        router = CredentialProviderRouter(
            profiles=profiles,
            native_store=LazyNativeCredentialStore(get_credential_vault),
            external_backends=backends,
        )
        return SignerCredentialSlots(router)
    except Exception:
        profiles.close()
        raise


def configure_credential_mode(args) -> int:
    profiles = _profiles()
    try:
        if args.mode == "native":
            if args.provider_id or args.secret_ref:
                raise ValueError("native_mode_does_not_accept_external_reference")
            if args.account_alias:
                profiles.set_account(args.account_alias, "native")
            else:
                profiles.set_default("native")
        else:
            if not args.provider_id or not args.secret_ref:
                raise ValueError("external_mode_requires_provider_and_secret_reference")
            backends = discover_external_vault_backends()
            if args.provider_id not in backends:
                raise CredentialProfileError("requested_external_provider_not_installed")
            if args.account_alias:
                profiles.set_account(
                    args.account_alias,
                    "external",
                    provider_id=args.provider_id,
                    secret_ref=args.secret_ref,
                )
            else:
                profiles.set_default(
                    "external",
                    provider_id=args.provider_id,
                    secret_ref=args.secret_ref,
                )
        target = (
            f"account {args.account_alias}" if args.account_alias else "global default"
        )
        print(f"Credential policy updated for {target}: {args.mode}.")
        return 0
    finally:
        profiles.close()


def store_exchange_credential(_args) -> int:
    account = _required("AITOS_SIGNER_ACCOUNT_ALIAS")
    key_id = input("Binance Futures Testnet API key: ").strip()
    secret = getpass.getpass("Binance Futures Testnet API secret (hidden): ")
    confirm = getpass.getpass("Confirm API secret (hidden): ")
    if secret != confirm:
        raise RuntimeError("exchange_secret_confirmation_mismatch")
    _slots().save(account, key_id=key_id, api_secret=secret)
    print(
        "Testnet credential stored using the configured signer credential provider."
    )
    return 0


def enroll_client(args) -> int:
    data = _data_dir()
    with SQLiteSignerRegistry(data / "client-registry.sqlite3") as registry:
        registry.enroll(
            identity=args.identity,
            account_alias=args.account_alias,
            allowed_symbols=set(args.symbols.split(",")),
            certificate_serial=args.serial,
            valid_from_ms=args.valid_from_ms,
            valid_until_ms=args.valid_until_ms,
            now_ms=args.now_ms,
        )
    print(
        "Client certificate enrolled. No private key or exchange credential was imported."
    )
    return 0


def revoke_client(args) -> int:
    data = _data_dir()
    with SQLiteSignerRegistry(data / "client-registry.sqlite3") as registry:
        registry.revoke(args.identity, args.serial, now_ms=args.now_ms)
    print("Client certificate serial revoked in the persistent signer registry.")
    return 0


def disable_client(args) -> int:
    data = _data_dir()
    with SQLiteSignerRegistry(data / "client-registry.sqlite3") as registry:
        registry.disable(args.identity, now_ms=args.now_ms)
    print("Signer client identity disabled.")
    return 0


async def _serve() -> None:
    data = _data_dir()
    account = _required("AITOS_SIGNER_ACCOUNT_ALIAS")
    symbols = _symbols()
    host = os.environ.get("AITOS_SIGNER_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("AITOS_SIGNER_PORT", "9443"))
    if (
        host in {"0.0.0.0", "::", ""}
        and os.environ.get("AITOS_SIGNER_ALLOW_WILDCARD_BIND") != "1"
    ):
        raise RuntimeError("wildcard_bind_requires_explicit_network_review")

    cert = Path(_required("AITOS_SIGNER_SERVER_CERT")).expanduser()
    key = Path(_required("AITOS_SIGNER_SERVER_KEY")).expanduser()
    client_ca = Path(_required("AITOS_SIGNER_CLIENT_CA")).expanduser()
    for material in (cert, key, client_ca):
        if material.is_symlink() or not material.is_file():
            raise RuntimeError("signer_tls_material_must_be_regular_files")
    if os.name == "posix" and key.stat().st_mode & 0o077:
        raise RuntimeError("signer_server_private_key_must_be_mode_0600")

    max_orders = int(os.environ.get("AITOS_SIGNER_MAX_ORDERS_PER_WINDOW", "10"))
    risk_limits = SignerRiskLimits(
        max_gross_exposure=_decimal_env("AITOS_SIGNER_MAX_GROSS_EXPOSURE"),
        max_symbol_exposure=_decimal_env("AITOS_SIGNER_MAX_SYMBOL_EXPOSURE"),
        max_daily_loss=_decimal_env("AITOS_SIGNER_MAX_DAILY_LOSS"),
        max_orders_per_window=max_orders,
    )
    policy = OrderIntentPolicy(
        account_alias=account,
        allowed_symbols=symbols,
        max_order_notional=_decimal_env("AITOS_SIGNER_MAX_ORDER_NOTIONAL"),
    )

    credentials = _slots()
    client = BinanceUSDMPaperClient(credentials=credentials, account_alias=account)
    exchange_evidence = BinanceUSDMPaperAccountEvidenceProvider(client=client)
    # Startup is read-only: prove the local credential and Testnet account path
    # before opening the RPC listener. No order is submitted here.
    credentials.resolve(account)
    exchange_evidence.get_snapshot(account, now_ms=client.clock_ms())
    risk_state = SQLiteSignerRiskState(
        upstream=exchange_evidence,
        database_path=data / "risk-state.sqlite3",
        max_orders_per_window=max_orders,
    )
    replay = SQLiteReplayStore(data / "intent-ledger.sqlite3")
    registry = SQLiteSignerRegistry(data / "client-registry.sqlite3")
    limiter = SQLiteSignerRateLimiter(
        data / "rpc-rate-limit.sqlite3",
        limit=int(os.environ.get("AITOS_SIGNER_RPC_RATE_LIMIT", str(max_orders))),
        window_ms=int(os.environ.get("AITOS_SIGNER_RPC_RATE_WINDOW_MS", "60000")),
        reduce_only_limit=int(
            os.environ.get("AITOS_SIGNER_REDUCE_ONLY_RPC_RATE_LIMIT", "100")
        ),
    )
    adapter = BinanceUSDMPaperExecutionAdapter(client=client)
    processor = PaperOnlyIntentProcessor(
        policy=policy,
        replay_store=replay,
        adapter=adapter,
        risk_limits=risk_limits,
        account_evidence_provider=risk_state,
    )
    server = PaperSignerRPCServer(
        ssl_context=build_server_context(
            certificate_chain=str(cert),
            private_key=str(key),
            trusted_client_ca=str(client_ca),
        ),
        registry=registry,
        rate_limiter=limiter,
        processor=processor,
    )
    recovery = PaperSignerRecoveryWorker(replay_store=replay, adapter=adapter)

    async def recovery_loop():
        while True:
            await asyncio.to_thread(recovery.run_once, now_ms=client.clock_ms())
            await asyncio.sleep(15)

    recovery_task = None
    try:
        # Resolve any crash-left DISPATCHING/UNKNOWN rows before accepting new work.
        await asyncio.to_thread(recovery.run_once, now_ms=client.clock_ms())
        await server.start(host, port)
        print(
            f"AITOS paper signer listening on {host}:{port} "
            "(TLS 1.3 mTLS, Binance Futures Testnet only)."
        )
        recovery_task = asyncio.create_task(recovery_loop())
        await server.serve_forever()
    finally:
        if recovery_task is not None:
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
        await server.close()
        limiter.close()
        registry.close()
        replay.close()
        risk_state.close()


def enable_client(args) -> int:
    data = _data_dir()
    with SQLiteSignerRegistry(data / "client-registry.sqlite3") as registry:
        registry.enable(args.identity, now_ms=args.now_ms)
    print("Signer client identity explicitly enabled.")
    return 0


def serve(_args) -> int:
    asyncio.run(_serve())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("store-testnet-credential").set_defaults(
        handler=store_exchange_credential
    )

    credential_mode = commands.add_parser(
        "set-credential-mode",
        help="Set global default or per-account native/external credential policy",
    )
    credential_mode.add_argument(
        "--mode", choices=("native", "external"), required=True
    )
    credential_mode.add_argument("--account-alias")
    credential_mode.add_argument("--provider-id")
    credential_mode.add_argument("--secret-ref")
    credential_mode.set_defaults(handler=configure_credential_mode)

    enroll = commands.add_parser("enroll-client")
    enroll.add_argument("--identity", required=True)
    enroll.add_argument("--account-alias", required=True)
    enroll.add_argument(
        "--symbols", required=True, help="Comma-separated uppercase symbols"
    )
    enroll.add_argument(
        "--serial", required=True, help="CA-issued certificate serial in hex"
    )
    enroll.add_argument("--valid-from-ms", required=True, type=int)
    enroll.add_argument("--valid-until-ms", required=True, type=int)
    enroll.add_argument("--now-ms", required=True, type=int)
    enroll.set_defaults(handler=enroll_client)

    revoke = commands.add_parser("revoke-client")
    revoke.add_argument("--identity", required=True)
    revoke.add_argument("--serial", required=True)
    revoke.add_argument("--now-ms", required=True, type=int)
    revoke.set_defaults(handler=revoke_client)

    disable = commands.add_parser("disable-client")
    disable.add_argument("--identity", required=True)
    disable.add_argument("--now-ms", required=True, type=int)
    disable.set_defaults(handler=disable_client)

    enable = commands.add_parser("enable-client")
    enable.add_argument("--identity", required=True)
    enable.add_argument("--now-ms", required=True, type=int)
    enable.set_defaults(handler=enable_client)

    commands.add_parser("serve").set_defaults(handler=serve)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
