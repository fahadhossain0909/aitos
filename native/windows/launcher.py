"""Native Windows session manager. Opens the authenticated console in a browser.
Never starts a trading runner. Credentials stay in Windows Credential Manager.
"""

import asyncio
import sys
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import simpledialog, messagebox

from aitos.console_gateway import (
    GatewayConfig,
    create_gateway,
    is_valid_operator_password,
)
from aitos.native.credential_vault import get_credential_vault
from aitos.native.console_host import LocalConsoleHost, exercise_local_host


def main():
    root = tk.Tk()
    root.title("AITOS — local read-only console")
    root.geometry("460x210")
    label = tk.Label(
        root, text="Windows vault · authenticated read-only console", wraplength=420
    )
    label.pack(pady=18)
    try:
        vault = get_credential_vault()
        password = vault.get("console/operator-password")
        if password is None:
            password = simpledialog.askstring(
                "AITOS setup",
                "Choose an operator password (24–1024 UTF-8 bytes)",
                show="*",
                parent=root,
            )
            if password is None:
                root.destroy()
                return
            confirmation = simpledialog.askstring(
                "Confirm", "Repeat operator password", show="*", parent=root
            )
            if password != confirmation or not is_valid_operator_password(password):
                raise ValueError()
            vault.put("console/operator-password", password)
        port = simpledialog.askinteger(
            "Local backend",
            "Existing local paper backend port (runner is NOT started)",
            initialvalue=8090,
            minvalue=1,
            maxvalue=65535,
            parent=root,
        )
        if port is None:
            root.destroy()
            return
        slot = f"console/backend-token/{port}"
        token = vault.get(slot)
        if token is None:
            token = simpledialog.askstring(
                "Backend authentication",
                "Backend bearer token (at least 32 characters)",
                show="*",
                parent=root,
            )
            if token is None:
                root.destroy()
                return
            if len(token) < 32 or "\r" in token or "\n" in token:
                raise ValueError()
            vault.put(slot, token)
    except Exception:
        messagebox.showerror(
            "AITOS",
            "Vault/setup failed. No server was started. Credentials are never saved "
            "to a plaintext fallback.",
            parent=root,
        )
        root.destroy()
        return
    dist = (
        Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        / "console_dist"
    )
    if not dist.is_dir():
        messagebox.showerror(
            "AITOS",
            "Packaged console assets missing. Use the reviewed Windows build.",
            parent=root,
        )
        root.destroy()
        return
    host = LocalConsoleHost(dist, upstream_port=port, password=password, token=token)
    host.start()

    def close():
        if not host.stop():
            messagebox.showerror(
                "AITOS", "Shutdown still draining; please retry.", parent=root
            )
            return
        root.destroy()

    def opened():
        if not host.ready.is_set():
            root.after(100, opened)
            return
        if host.failed:
            label.config(
                text="Local gateway could not start. No trading runner was started."
            )
            return
        label.config(
            text="Read-only console ready. Closing this manager revokes its sessions."
        )
        tk.Button(
            root,
            text="Open authenticated console",
            command=lambda: webbrowser.open(host.origin),
        ).pack(pady=8)
        webbrowser.open(host.origin)

    root.protocol("WM_DELETE_WINDOW", close)
    tk.Button(root, text="Close console and revoke sessions", command=close).pack(
        pady=8
    )
    root.after(100, opened)
    root.mainloop()


def self_test():
    """Frozen-bundle smoke: native API, assets, gateway imports; no server launch."""
    import uuid
    import secrets

    vault = get_credential_vault()
    if vault.get("tests/self-test-" + uuid.uuid4().hex) is not None:
        raise RuntimeError("unexpected test credential")
    dist = (
        Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        / "console_dist"
    )
    create_gateway(
        GatewayConfig("http://127.0.0.1:8780", "http://127.0.0.1:8090", dist),
        password=secrets.token_urlsafe(32),
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        try:
            self_test()
        except Exception:
            raise SystemExit(2) from None
    elif sys.argv[1:] == ["--gateway-self-test"]:
        try:
            self_test()
            dist = (
                Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
                / "console_dist"
            )
            asyncio.run(exercise_local_host(dist))
        except Exception:
            raise SystemExit(2) from None
    elif sys.argv[1:]:
        raise SystemExit(2)
    else:
        main()
