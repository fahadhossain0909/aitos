from pathlib import Path

from aitos.config.paths import (
    get_model_data_dir,
    get_project_root,
    resolve_project_path,
)


def test_model_data_dir_respects_explicit_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom-models"
    monkeypatch.setenv("MODEL_DATA_DIR", str(custom))
    assert get_model_data_dir() == custom.resolve()


def test_project_root_respects_aitos_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AITOS_HOME", str(tmp_path))
    assert get_project_root() == tmp_path.resolve()


def test_native_model_data_defaults_inside_project(monkeypatch, tmp_path):
    monkeypatch.delenv("MODEL_DATA_DIR", raising=False)
    monkeypatch.setenv("AITOS_HOME", str(tmp_path))
    monkeypatch.setenv("AITOS_ENV_FILE", str(tmp_path / "missing.env"))
    original_exists = Path.exists

    def local_exists(path):
        if path == Path("/.dockerenv"):
            return False
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", local_exists)
    assert get_model_data_dir() == tmp_path.resolve() / "data" / "models"


def test_relative_paths_resolve_from_project_root(monkeypatch, tmp_path):
    monkeypatch.setenv("AITOS_HOME", str(tmp_path))
    assert (
        resolve_project_path("data/models/checkpoint.pkl")
        == (tmp_path / "data/models/checkpoint.pkl").resolve()
    )
