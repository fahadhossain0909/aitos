"""Portable AITOS filesystem locations with explicit, overridable roots."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def get_project_root() -> Path:
    configured = os.getenv("AITOS_HOME")
    root = (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[2]
    )
    return root.resolve()


def resolve_project_path(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = get_project_root() / candidate
    return candidate.resolve()


def _override(name: str) -> Path | None:
    value = os.getenv(name)
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = get_project_root() / path
    return path.resolve()


def get_data_dir() -> Path:
    if configured := _override("AITOS_DATA_DIR"):
        return configured
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return (base / "AITOS").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library/Application Support/AITOS").resolve()
    base = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local/share"))
    return (base.expanduser() / "aitos").resolve()


def get_cache_dir() -> Path:
    if configured := _override("AITOS_CACHE_DIR"):
        return configured
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return (base / "AITOS/Cache").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library/Caches/AITOS").resolve()
    base = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
    return (base.expanduser() / "aitos").resolve()


def get_config_dir() -> Path:
    if configured := _override("AITOS_CONFIG_DIR"):
        return configured
    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA", Path.home() / "AppData/Roaming"))
        return (base / "AITOS").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library/Application Support/AITOS/config").resolve()
    base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return (base.expanduser() / "aitos").resolve()


def get_logs_dir() -> Path:
    return _override("AITOS_LOG_DIR") or (get_data_dir() / "logs")


def ensure_application_directories() -> dict[str, Path]:
    paths = {
        "data": get_data_dir(),
        "cache": get_cache_dir(),
        "config": get_config_dir(),
        "logs": get_logs_dir(),
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def get_model_data_dir() -> Path:
    from dotenv import load_dotenv

    env_file = get_env_file()
    if env_file.is_file():
        load_dotenv(env_file, override=False)
    configured = os.getenv("MODEL_DATA_DIR")
    if configured:
        return resolve_project_path(configured)
    if Path("/.dockerenv").exists():
        return Path("/models")
    # Preserve an existing checkout's model data during the storage transition.
    legacy = get_project_root() / "data" / "models"
    if legacy.is_dir():
        return legacy.resolve()
    return (get_data_dir() / "models").resolve()


def get_env_file() -> Path:
    configured = os.getenv("AITOS_ENV_FILE")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = get_project_root() / candidate
        return candidate.resolve()
    return get_project_root() / ".env"
