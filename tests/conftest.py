import pytest

from carddues import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A database in a throwaway CARDDUES_HOME."""
    monkeypatch.setenv("CARDDUES_HOME", str(tmp_path / "home"))
    # Do not pull the repo seed into unit-test DBs.
    monkeypatch.setenv("CARDDUES_CATEGORIES_SEED", str(tmp_path / "no-seed.json"))
    connection = db.connect()
    db.init(connection)
    yield connection
    connection.close()
