"""Dependency profiles must not overwrite the same HTTP mock import package."""

from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


@pytest.mark.parametrize("profile", ["requirements.txt", "requirements-backtest.txt"])
def test_http_mock_profiles_use_the_same_maintained_distribution(profile):
    root = Path(__file__).resolve().parents[1]
    names = {
        canonicalize_name(Requirement(line.strip()).name)
        for line in (root / profile).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "aioresponses-ng" in names
    assert "aioresponses" not in names
