"""Portable filesystem paths shared by local and container runtimes.

AITOS_HOME selects the checkout root; MODEL_DATA_DIR explicitly overrides model
storage. Docker keeps using /models, while native/local runs default to a
repository-local data/models directory so they do not require root permissions.
"""

from __future__ import annotations

import os
from pathlib import Path


def get_project_root() -> Path:
    configured = os.getenv("AITOS_HOME")
    root = (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[2]
    )
    return root.resolve()


def get_model_data_dir() -> Path:
    # Load repository-local defaults before modules resolve paths at import time.
    from dotenv import load_dotenv

    env_file = get_env_file()
    if env_file.is_file():
        load_dotenv(env_file, override=False)

    configured = os.getenv("MODEL_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()

    # Docker images provide /.dockerenv and mount the persistent model volume at
    # /models. Native Windows/Linux/macOS runs should keep state in the checkout.
    if Path("/.dockerenv").exists():
        return Path("/models")

    return get_project_root() / "data" / "models"


def get_env_file() -> Path:
    configured = os.getenv("AITOS_ENV_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    return get_project_root() / ".env"
