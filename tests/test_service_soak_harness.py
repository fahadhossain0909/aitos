import pytest

from engineering.tools.paper_service_soak import guard


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"AITOS_SERVICE_TEST_ACK": "yes"},
        {
            "AITOS_SERVICE_TEST_ACK": "isolated-test-services-only",
            "AITOS_SERVICE_TEST_PORT": "80",
        },
        {
            "AITOS_SERVICE_TEST_ACK": "isolated-test-services-only",
            "AITOS_SERVICE_TEST_PORT": "-1",
        },
        {
            "AITOS_SERVICE_TEST_ACK": "isolated-test-services-only",
            "AITOS_SERVICE_TEST_PORT": "abc",
        },
    ],
)
def test_guard_blocks_unacknowledged_or_nondedicated_port(env):
    with pytest.raises(ValueError):
        guard(env)


def test_acknowledged_service_guard():
    assert guard({"AITOS_SERVICE_TEST_ACK": "isolated-test-services-only"}) == 18123


@pytest.mark.parametrize("phase", [None, True, "unknown"])
async def test_unknown_phase_rejected_before_network_or_directory_creation(
    tmp_path, phase
):
    from engineering.tools.paper_service_soak import run

    root = tmp_path / "must-not-exist"
    with pytest.raises(ValueError):
        await run(phase, root, 1)
    assert not root.exists()


def test_trace_metadata_has_no_environment_secret(monkeypatch):
    from engineering.tools.paper_service_soak import evidence_metadata

    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("AITOS_CONSOLE_TOKEN", "must-not-leak")
    report = evidence_metadata("seed")
    assert report["source_commit"] == "a" * 40 and report["synthetic"] is True
    assert "must-not-leak" not in str(report)
    monkeypatch.setenv("GITHUB_SHA", "untrusted input")
    assert evidence_metadata("seed")["source_commit"] == "not_recorded"
