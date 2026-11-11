import asyncio
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from aitos.security.mtls import build_client_context, build_server_context
from aitos.security.paper_processor import PaperOnlyIntentProcessor
from aitos.security.replay_store import SQLiteReplayStore
from aitos.security.risk_limits import AccountRiskSnapshot, SignerRiskLimits
from aitos.security.signing_policy import OrderIntentPolicy
from aitos.security.signer_rpc_client import SignerRPCClient
from aitos.security.signer_service import (
    PaperSignerRPCServer,
    SQLiteSignerRateLimiter,
    SQLiteSignerRegistry,
)


NOW = 1_800_000_000_000
IDENTITY = "spiffe://aitos/signer/client-01"


def _openssl(*args):
    subprocess.run(
        ["openssl", *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
    )


def _certificates(directory: Path):
    ca_key, ca_cert = directory / "ca.key", directory / "ca.crt"
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        ca_key,
        "-out",
        ca_cert,
        "-subj",
        "/CN=AITOS-Test-CA",
        "-days",
        "365",
    )
    for role, serial, san, usage in (
        ("server", "0xAABB", "DNS:localhost", "serverAuth"),
        ("client", "0xA1B2", f"URI:{IDENTITY}", "clientAuth"),
    ):
        key, csr, cert = (
            directory / f"{role}.key",
            directory / f"{role}.csr",
            directory / f"{role}.crt",
        )
        _openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            key,
            "-out",
            csr,
            "-subj",
            f"/CN=aitos-{role}",
        )
        ext = directory / f"{role}.ext"
        ext.write_text(
            f"subjectAltName={san}\nextendedKeyUsage={usage}\n",
            encoding="utf-8",
        )
        _openssl(
            "x509",
            "-req",
            "-in",
            csr,
            "-CA",
            ca_cert,
            "-CAkey",
            ca_key,
            "-set_serial",
            serial,
            "-out",
            cert,
            "-days",
            "365",
            "-extfile",
            ext,
        )
    return (
        ca_cert,
        directory / "server.crt",
        directory / "server.key",
        directory / "client.crt",
        directory / "client.key",
    )


@pytest.mark.skipif(shutil.which("openssl") is None, reason="OpenSSL CLI required")
def test_real_tls13_mtls_socket_accepts_only_enrolled_paper_intents(tmp_path):
    ca, server_cert, server_key, client_cert, client_key = _certificates(tmp_path)
    server_context = build_server_context(
        certificate_chain=str(server_cert),
        private_key=str(server_key),
        trusted_client_ca=str(ca),
    )
    client_context = build_client_context(
        certificate_chain=str(client_cert),
        private_key=str(client_key),
        trusted_server_ca=str(ca),
    )

    policy = OrderIntentPolicy(
        account_alias="paper-primary",
        allowed_symbols=frozenset({"BTCUSDT"}),
        max_order_notional=Decimal("1000"),
    )
    snapshot = AccountRiskSnapshot(
        account_alias="paper-primary",
        captured_at_ms=NOW,
        gross_exposure=Decimal("0"),
        symbol_exposure={},
        daily_loss=Decimal("0"),
        order_count_window=0,
        positions={},
    )

    class Evidence:
        def get_snapshot(self, account_alias, *, now_ms):
            assert account_alias == "paper-primary"
            return snapshot

    async def scenario():
        registry = SQLiteSignerRegistry(tmp_path / "registry.sqlite3")
        registry.enroll(
            identity=IDENTITY,
            account_alias="paper-primary",
            allowed_symbols={"BTCUSDT"},
            certificate_serial="A1B2",
            valid_from_ms=NOW - 1000,
            valid_until_ms=NOW + 100_000,
            now_ms=NOW,
        )
        replay = SQLiteReplayStore(tmp_path / "replay.sqlite3")
        limiter = SQLiteSignerRateLimiter(
            tmp_path / "rate.sqlite3", limit=10, window_ms=60_000
        )
        processor = PaperOnlyIntentProcessor(
            policy=policy,
            replay_store=replay,
            risk_limits=SignerRiskLimits(
                max_gross_exposure=Decimal("1000"),
                max_symbol_exposure=Decimal("500"),
                max_daily_loss=Decimal("100"),
                max_orders_per_window=10,
            ),
            account_evidence_provider=Evidence(),
        )
        server = PaperSignerRPCServer(
            ssl_context=server_context,
            registry=registry,
            rate_limiter=limiter,
            processor=processor,
            clock_ms=lambda: NOW,
        )
        try:
            await server.start("127.0.0.1", 0)
            port = server._server.sockets[0].getsockname()[1]
            client = SignerRPCClient(
                ssl_context=client_context,
                host="127.0.0.1",
                port=port,
                server_hostname="localhost",
            )
            intent = {
                "schema_version": 1,
                "intent_id": "intent-mtls-001",
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
                "client_order_id": "aitos-mtls-001",
            }
            response = await client.submit(
                intent,
                account_alias="paper-primary",
                sent_at_ms=NOW,
                request_id="rpc-mtls-001",
            )
            assert response.status == "accepted"
            assert response.outcome_code == "paper_order_recorded"
            assert response.duplicate is False
        finally:
            await server.close()
            limiter.close()
            replay.close()
            registry.close()

    asyncio.run(scenario())
