"""Regression: `.env` must be found from the repo root even when the app starts from backend/."""
from pathlib import Path

from app import config
from app.config import ENV_FILES, Settings

CANARY = "canary-root-key-0123456789"


def test_repo_root_env_is_first_and_absolute():
    root_env = ENV_FILES[0]
    assert root_env.is_absolute()
    assert (root_env.parent / "pyproject.toml").exists()


def test_root_env_found_from_other_cwd_and_cwd_overrides(tmp_path, monkeypatch):
    root, cwd = tmp_path / "root", tmp_path / "elsewhere"
    root.mkdir()
    cwd.mkdir()
    (root / ".env").write_text(f"HORNET_ADMIN_API_KEY={CANARY}\nHORNET_ENV=dev\n", encoding="utf-8")
    monkeypatch.delenv("HORNET_ADMIN_API_KEY", raising=False)
    monkeypatch.chdir(cwd)
    files = (root / ".env", Path(".env"))
    assert Settings(_env_file=files).admin_api_key == CANARY
    (cwd / ".env").write_text("HORNET_ADMIN_API_KEY=cwd-override-key-0123456789\n", encoding="utf-8")
    assert Settings(_env_file=files).admin_api_key == "cwd-override-key-0123456789"
    assert config.Settings.model_config["env_file"] == ENV_FILES


def test_relative_sqlite_url_is_anchored_to_repo_root(tmp_path, monkeypatch):
    from app.config import _REPO_ROOT, resolve_database_url

    monkeypatch.chdir(tmp_path)
    expected = "sqlite:///" + (_REPO_ROOT / "hornet_dev.db").as_posix()
    assert resolve_database_url("sqlite:///./hornet_dev.db") == expected
    assert resolve_database_url("sqlite:///hornet_dev.db") == expected


def test_other_database_urls_unchanged():
    from app.config import resolve_database_url

    for url in ("sqlite://", "sqlite:///:memory:", "sqlite:////abs/path.db", "sqlite:///C:/x/y.db",
                "postgresql+psycopg://u:p@h/db"):
        assert resolve_database_url(url) == url
