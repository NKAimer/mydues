"""Config home paths and legacy ~/.carddues migration."""

from pathlib import Path

from mydues import config


def test_migrates_legacy_home_and_db(tmp_path, monkeypatch):
    monkeypatch.delenv("MYDUES_HOME", raising=False)
    monkeypatch.delenv("CARDDUES_HOME", raising=False)
    monkeypatch.delenv("MYDUES_CREDENTIALS", raising=False)
    monkeypatch.delenv("CARDDUES_CREDENTIALS", raising=False)

    legacy = tmp_path / ".carddues"
    new_home = tmp_path / ".mydues"
    legacy.mkdir()
    (legacy / "carddues.db").write_text("db", encoding="utf-8")
    (legacy / "token.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(config, "DEFAULT_HOME", new_home)
    monkeypatch.setattr(config, "_LEGACY_HOME", legacy)
    monkeypatch.setattr(config, "_migrated", False)

    assert config.home() == new_home
    assert new_home.is_dir()
    assert not legacy.exists()
    assert (new_home / "mydues.db").read_text(encoding="utf-8") == "db"
    assert not (new_home / "carddues.db").exists()
    assert (new_home / "token.json").exists()


def test_renames_legacy_db_inside_mydues_home(tmp_path, monkeypatch):
    monkeypatch.delenv("MYDUES_HOME", raising=False)
    monkeypatch.delenv("CARDDUES_HOME", raising=False)

    new_home = tmp_path / ".mydues"
    new_home.mkdir()
    (new_home / "carddues.db").write_text("db", encoding="utf-8")

    monkeypatch.setattr(config, "DEFAULT_HOME", new_home)
    monkeypatch.setattr(config, "_LEGACY_HOME", tmp_path / ".carddues")
    monkeypatch.setattr(config, "_migrated", False)

    assert config.db_path() == new_home / "mydues.db"
    assert (new_home / "mydues.db").exists()
    assert not (new_home / "carddues.db").exists()


def test_mydues_home_env_skips_default_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("MYDUES_HOME", str(tmp_path / "custom"))
    legacy = tmp_path / ".carddues"
    legacy.mkdir()
    (legacy / "carddues.db").write_text("db", encoding="utf-8")

    monkeypatch.setattr(config, "DEFAULT_HOME", tmp_path / ".mydues")
    monkeypatch.setattr(config, "_LEGACY_HOME", legacy)
    monkeypatch.setattr(config, "_migrated", False)

    assert config.home() == tmp_path / "custom"
    assert legacy.exists()
    assert (legacy / "carddues.db").exists()
