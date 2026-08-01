"""Filesystem locations and tunable thresholds."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

DEFAULT_HOME = Path.home() / ".mydues"
_LEGACY_HOME = Path.home() / ".carddues"
_LEGACY_DB_NAME = "carddues.db"
_DB_NAME = "mydues.db"

_migrated = False


def _env(*names: str) -> str | None:
    """Return the first set environment variable among *names*."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _migrate_legacy_home() -> None:
    """Move ~/.carddues → ~/.mydues and carddues.db → mydues.db once."""
    global _migrated
    if _migrated:
        return
    _migrated = True

    # Only auto-migrate the default layout (no MYDUES_HOME / CARDDUES_HOME override).
    if _env("MYDUES_HOME", "CARDDUES_HOME"):
        return

    if not DEFAULT_HOME.exists() and _LEGACY_HOME.exists():
        try:
            _LEGACY_HOME.rename(DEFAULT_HOME)
        except OSError:
            return

    target = DEFAULT_HOME
    if not target.is_dir():
        return
    legacy_db = target / _LEGACY_DB_NAME
    new_db = target / _DB_NAME
    if legacy_db.exists() and not new_db.exists():
        try:
            legacy_db.rename(new_db)
        except OSError:
            pass


def home() -> Path:
    _migrate_legacy_home()
    override = _env("MYDUES_HOME", "CARDDUES_HOME")
    if override:
        return Path(override).expanduser()
    return DEFAULT_HOME


def db_path() -> Path:
    return home() / _DB_NAME


def credentials_path() -> Path:
    """Google OAuth client file downloaded from Google Cloud Console."""
    override = _env("MYDUES_CREDENTIALS", "CARDDUES_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    return home() / "credentials.json"


def token_path() -> Path:
    return home() / "token.json"


def attachments_dir() -> Path:
    return home() / "attachments"


def categories_seed_path() -> Path:
    """Committed category phrases + merchant overrides (not the full DB).

    Override with MYDUES_CATEGORIES_SEED (CARDDUES_CATEGORIES_SEED still works).
    Default is ``data/categories.json`` next to the repo root when developing
    from a clone.
    """
    override = _env("MYDUES_CATEGORIES_SEED", "CARDDUES_CATEGORIES_SEED")
    if override:
        return Path(override).expanduser()
    # mydues/config.py → repo root / data/categories.json
    return Path(__file__).resolve().parent.parent / "data" / "categories.json"


def session_secret() -> str:
    """Key for signing the dashboard's session cookie.

    Persisted so that an OAuth redirect still validates after a restart.
    """
    path = home() / "session_secret"
    if not path.exists():
        ensure_dirs()
        path.write_text(secrets.token_hex(32))
        os.chmod(path, 0o600)
    return path.read_text().strip()


def ensure_dirs() -> None:
    home().mkdir(parents=True, exist_ok=True)
    attachments_dir().mkdir(parents=True, exist_ok=True)
    os.chmod(home(), 0o700)


# A statement older than this is no longer a trustworthy view of what you owe.
STALE_AFTER_DAYS = 40

# How far back to search Gmail on a full ingest.
DEFAULT_LOOKBACK_DAYS = 400
