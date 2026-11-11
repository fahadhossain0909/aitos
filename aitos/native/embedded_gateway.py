"""Private Tauri sidecar: read-only operator gateway plus local signer admin.

The operator gateway emits only a loopback origin. Signer credential requests
arrive only from the capability-scoped local signer window and travel to this
sidecar over stdin; they are never placed in argv, environment or stdout.
No trading process is started by this sidecar.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import signal
import sys
import threading

from aitos.console_capabilities import RuntimeEndpoint
from aitos.console_gateway import is_valid_operator_password
from aitos.native.console_host import LocalConsoleHost, exercise_local_host
from aitos.native.credential_vault import get_credential_vault


def assets_directory():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "console_dist"
    return Path(__file__).resolve().parents[2] / "desktop/dist"


def read_native_credentials():
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    try:
        vault = get_credential_vault()
        password = vault.get("console/operator-password")
        if password is None:
            password = simpledialog.askstring(
                "AITOS operator",
                "Choose operator password (24–1024 UTF-8 bytes)",
                show="*",
                parent=root,
            )
            if password is None:
                return None
            confirmation = simpledialog.askstring(
                "AITOS operator", "Repeat operator password", show="*", parent=root
            )
            if password != confirmation or not is_valid_operator_password(password):
                raise ValueError("operator password policy")
            vault.put("console/operator-password", password)
        if not is_valid_operator_password(password):
            raise ValueError("stored operator credential does not satisfy policy")
        origin = simpledialog.askstring(
            "AITOS runtime",
            "Exact HTTPS runtime origin or local SSH-tunnel origin",
            initialvalue="http://127.0.0.1:8090",
            parent=root,
        )
        if origin is None:
            return None
        endpoint = RuntimeEndpoint(origin)
        token = vault.get(endpoint.token_slot)
        if token is None:
            token = simpledialog.askstring(
                "AITOS runtime",
                "Backend bearer token (stored only in OS vault)",
                show="*",
                parent=root,
            )
            if token is None:
                return None
            if len(token) < 32 or "\r" in token or "\n" in token:
                raise ValueError("backend bearer policy")
            vault.put(endpoint.token_slot, token)
        return endpoint, password, token
    finally:
        root.destroy()


def run_embedded():
    credentials = read_native_credentials()
    if credentials is None:
        return
    endpoint, password, token = credentials
    host = LocalConsoleHost(
        assets_directory(),
        upstream_origin=endpoint.origin,
        password=password,
        token=token,
    )
    stopping = threading.Event()
    for kind in (signal.SIGTERM, signal.SIGINT):
        signal.signal(kind, lambda *_: stopping.set())
    try:
        host.start()
        if not host.ready.wait(15) or host.failed or host.origin is None:
            raise RuntimeError("gateway unavailable")
        print("AITOS_GATEWAY_ORIGIN=" + host.origin, flush=True)
        while not stopping.wait(0.25):
            if host.closed.is_set():
                raise RuntimeError("gateway terminated")
    finally:
        if not host.stop():
            raise RuntimeError("gateway shutdown not confirmed")


def main():
    try:
        if sys.argv[1:] == ["--gateway-self-test"]:
            # Actual TCP authentication/revocation against synthetic backend.
            asyncio.run(exercise_local_host(assets_directory()))
        elif sys.argv[1:] == ["--embedded"]:
            run_embedded()
        elif sys.argv[1:] == ["--credential-admin"]:
            from aitos.native.signer_admin import main as signer_admin_main

            return signer_admin_main()
        else:
            return 2
    except Exception:
        # Do not expose secrets/provider exceptions/paths in child output.
        print(
            "AITOS sidecar unavailable; check OS vault, endpoint and package",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
