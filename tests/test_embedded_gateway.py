from types import SimpleNamespace

import pytest

from aitos.console_capabilities import RuntimeEndpoint
from aitos.native import embedded_gateway as gateway


def test_unknown_args_never_start_gateway(monkeypatch):
    monkeypatch.setattr(gateway.sys, "argv", ["sidecar", "--start-live"])
    monkeypatch.setattr(gateway, "run_embedded", lambda: pytest.fail("must not run"))
    assert gateway.main() == 2


def test_cancel_does_not_start_or_emit_origin(monkeypatch, capsys):
    monkeypatch.setattr(gateway, "read_native_credentials", lambda: None)
    monkeypatch.setattr(
        gateway, "LocalConsoleHost", lambda *a, **kw: pytest.fail("must not start")
    )
    gateway.run_embedded()
    assert capsys.readouterr().out == ""


def test_sidecar_failure_does_not_print_credentials_or_provider_error(
    monkeypatch, capsys
):
    monkeypatch.setattr(gateway.sys, "argv", ["sidecar", "--embedded"])

    def fail():
        raise RuntimeError("sensitive-token synthetic provider exception")

    monkeypatch.setattr(gateway, "run_embedded", fail)
    assert gateway.main() == 2
    output = capsys.readouterr()
    assert "sensitive-token" not in output.err + output.out
    assert "provider exception" not in output.err + output.out


def test_startup_failure_stops_host_without_publishing_origin(monkeypatch, capsys):
    stopped = []
    host = SimpleNamespace(
        start=lambda: None,
        ready=SimpleNamespace(wait=lambda timeout: True),
        failed=True,
        origin="http://127.0.0.1:1234",
        stop=lambda: stopped.append(True) or True,
    )
    monkeypatch.setattr(
        gateway,
        "read_native_credentials",
        lambda: (
            RuntimeEndpoint("https://runtime.example"),
            "synthetic-password",
            "synthetic-token",
        ),
    )
    monkeypatch.setattr(gateway, "LocalConsoleHost", lambda *a, **kw: host)
    monkeypatch.setattr(gateway.signal, "signal", lambda *args: None)
    with pytest.raises(RuntimeError, match="gateway unavailable"):
        gateway.run_embedded()
    assert stopped == [True]
    assert capsys.readouterr().out == ""


def test_lifecycle_emits_only_origin_then_shutdown(monkeypatch, capsys):
    stopped = []
    # Simulate gateway failure immediately after ready. Not runtime-health evidence.
    host = SimpleNamespace(
        start=lambda: None,
        ready=SimpleNamespace(wait=lambda timeout: True),
        failed=False,
        origin="http://127.0.0.1:1234",
        closed=SimpleNamespace(is_set=lambda: True),
        stop=lambda: stopped.append(True) or True,
    )
    monkeypatch.setattr(
        gateway,
        "read_native_credentials",
        lambda: (
            RuntimeEndpoint("https://runtime.example"),
            "synthetic-password",
            "synthetic-token",
        ),
    )
    monkeypatch.setattr(gateway, "LocalConsoleHost", lambda *a, **kw: host)
    monkeypatch.setattr(gateway.signal, "signal", lambda *args: None)
    with pytest.raises(RuntimeError, match="gateway terminated"):
        gateway.run_embedded()
    assert stopped == [True]
    assert capsys.readouterr().out == "AITOS_GATEWAY_ORIGIN=http://127.0.0.1:1234\n"
