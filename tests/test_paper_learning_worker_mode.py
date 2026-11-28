"""The paper runtime delegates continual learning to its dedicated service by default."""

import pytest

from run_paper_trading import _embedded_learning_worker_enabled


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("false", False),
        ("0", False),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
    ],
)
def test_embedded_learning_worker_requires_explicit_opt_in(
    monkeypatch, value, expected
):
    name = "AITOS_EMBEDDED_LEARNING_WORKER_ENABLED"
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    assert _embedded_learning_worker_enabled() is expected
