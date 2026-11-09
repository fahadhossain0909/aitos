from pathlib import Path

from aitos.config import paths


def _hide_docker_marker(monkeypatch):
    exists = Path.exists
    is_dir = Path.is_dir
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: False if self == Path("/.dockerenv") else exists(self),
    )
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda self: False if self == Path("/.dockerenv") else is_dir(self),
    )


def test_linux_xdg_paths_and_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    for name in (
        "AITOS_DATA_DIR",
        "AITOS_CACHE_DIR",
        "AITOS_CONFIG_DIR",
        "AITOS_LOG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    assert paths.get_data_dir() == (tmp_path / "xdg-data/aitos").resolve()
    assert paths.get_cache_dir() == (tmp_path / "xdg-cache/aitos").resolve()
    assert paths.get_config_dir() == (tmp_path / "xdg-config/aitos").resolve()
    assert paths.get_logs_dir() == (tmp_path / "xdg-data/aitos/logs").resolve()


def test_explicit_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("AITOS_DATA_DIR", str(tmp_path / "durable"))
    monkeypatch.setenv("AITOS_CACHE_DIR", "cache")
    monkeypatch.setenv("AITOS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AITOS_LOG_DIR", str(tmp_path / "logs"))
    assert paths.get_data_dir() == (tmp_path / "durable").resolve()
    assert paths.get_cache_dir() == (paths.get_project_root() / "cache").resolve()
    assert paths.get_config_dir() == (tmp_path / "config").resolve()
    assert paths.get_logs_dir() == (tmp_path / "logs").resolve()


def test_existing_legacy_model_directory_is_preserved(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    legacy = root / "data/models"
    legacy.mkdir(parents=True)
    monkeypatch.setenv("AITOS_HOME", str(root))
    monkeypatch.delenv("MODEL_DATA_DIR", raising=False)
    _hide_docker_marker(monkeypatch)
    assert paths.get_model_data_dir() == legacy.resolve()


def test_model_directory_defaults_to_user_data(monkeypatch, tmp_path):
    monkeypatch.setenv("AITOS_HOME", str(tmp_path / "repo"))
    monkeypatch.setenv("AITOS_DATA_DIR", str(tmp_path / "aitos-data"))
    monkeypatch.delenv("MODEL_DATA_DIR", raising=False)
    _hide_docker_marker(monkeypatch)
    assert paths.get_model_data_dir() == (tmp_path / "aitos-data/models").resolve()


def test_explicit_directory_creation(monkeypatch, tmp_path):
    for name in (
        "AITOS_DATA_DIR",
        "AITOS_CACHE_DIR",
        "AITOS_CONFIG_DIR",
        "AITOS_LOG_DIR",
    ):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    result = paths.ensure_application_directories()
    assert set(result) == {"data", "cache", "config", "logs"}
    assert all(path.is_dir() for path in result.values())


def test_windows_and_macos_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(paths.sys, "platform", "win32")
    assert paths.get_data_dir() == (tmp_path / "Local/AITOS").resolve()
    assert paths.get_cache_dir() == (tmp_path / "Local/AITOS/Cache").resolve()
    assert paths.get_config_dir() == (tmp_path / "Roaming/AITOS").resolve()

    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda *_: tmp_path)
    assert paths.get_data_dir() == (
        tmp_path / "Library/Application Support/AITOS"
    ).resolve()
    assert paths.get_cache_dir() == (tmp_path / "Library/Caches/AITOS").resolve()
    assert paths.get_config_dir() == (
        tmp_path / "Library/Application Support/AITOS/config"
    ).resolve()


def test_explicit_application_data_root_overrides_legacy_model_path(
    monkeypatch, tmp_path
):
    root = tmp_path / "repo"
    (root / "data/models").mkdir(parents=True)
    monkeypatch.setenv("AITOS_HOME", str(root))
    monkeypatch.setenv("AITOS_DATA_DIR", str(tmp_path / "selected-data"))
    monkeypatch.delenv("MODEL_DATA_DIR", raising=False)
    _hide_docker_marker(monkeypatch)
    assert paths.get_model_data_dir() == (
        tmp_path / "selected-data/models"
    ).resolve()
