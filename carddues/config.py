"""Filesystem locations and tunable thresholds."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

DEFAULT_HOME = Path.home() / ".carddues"


def home() -> Path:
    return Path(os.environ.get("CARDDUES_HOME", DEFAULT_HOME)).expanduser()


def db_path() -> Path:
    return home() / "carddues.db"


def credentials_path() -> Path:
    """Google OAuth client file downloaded from Google Cloud Console."""
    return Path(os.environ.get("CARDDUES_CREDENTIALS", home() / "credentials.json")).expanduser()


def token_path() -> Path:
    return home() / "token.json"


def attachments_dir() -> Path:
    return home() / "attachments"


def categories_seed_path() -> Path:
    """Committed category phrases + merchant overrides (not the full DB).

    Override with CARDDUES_CATEGORIES_SEED. Default is ``data/categories.json``
    next to the repo root when developing from a clone.
    """
    override = os.environ.get("CARDDUES_CATEGORIES_SEED")
    if override:
        return Path(override).expanduser()
    # carddues/config.py → repo root / data/categories.json
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
