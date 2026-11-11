"""Actual loopback TCP and threading, synthetic credentials, no Windows claim."""

import pytest

from aitos.native.console_host import LocalConsoleHost, exercise_local_host


@pytest.fixture
def dist(tmp_path):
    (tmp_path / "index.html").write_text("<html>synthetic fixture</html>")
    return tmp_path


async def test_real_native_host_auth_proxy_and_shutdown(dist):
    result = await exercise_local_host(dist)
    assert result["synthetic"] and result["status"] == "passed"
    assert not result["live_authorized"] and not result["production_certified"]


@pytest.mark.parametrize("attempt", range(5))
def test_stop_during_start_is_retained_and_releases_thread(dist, attempt):
    host = LocalConsoleHost(
        dist, upstream_port=8090, password="test-only-password-long", token="x" * 32
    )
    host.start()
    assert host.stop(5)
    assert host.closed.is_set() and not host._thread.is_alive()
    assert host._password is None and host._token is None
    assert host.stop(5)


def test_failed_startup_exposes_failure_and_closes_thread(tmp_path):
    host = LocalConsoleHost(
        tmp_path / "missing", upstream_port=8090, password="short", token="x" * 32
    )
    host.start()
    assert host.ready.wait(5)
    assert host.stop(5) and host.failed


def test_stop_before_start_prevents_restart(dist):
    host = LocalConsoleHost(
        dist, upstream_port=8090, password="test-only-password-long", token="x" * 32
    )
    assert host.stop()
    with pytest.raises(RuntimeError):
        host.start()


@pytest.mark.parametrize("port", [True, 0, 65536, "8090"])
def test_invalid_port_has_no_thread_or_socket(dist, port):
    with pytest.raises(ValueError):
        LocalConsoleHost(dist, upstream_port=port, password="unused", token=None)
