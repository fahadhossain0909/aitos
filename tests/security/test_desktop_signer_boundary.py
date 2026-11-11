"""Security boundary tests for the remote operator console.

The operator WebView is intentionally an external loopback origin and must not
receive native signer or credential-vault IPC privileges.
"""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_operator_tauri_shell_exposes_no_native_ipc_capabilities():
    config = json.loads(
        (ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["app"]["security"]["capabilities"] == []

    rust = (ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text(
        encoding="utf-8"
    )
    assert "invoke_handler" not in rust
    assert "generate_handler!" not in rust
    assert "WebviewUrl::External(url)" in rust
    assert 'url.host_str() == Some("127.0.0.1")' in rust
    assert ".on_navigation(move |next| next.origin() == allowed)" in rust


def test_operator_credential_screen_is_only_an_unapplied_metadata_draft():
    app = (ROOT / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "UNAPPLIED_CREDENTIAL_POLICY_DRAFT" in app
    assert "Credential custody draft saved locally only." in app
    assert "The signer policy was not changed." in app
    assert "invoke(" not in app
