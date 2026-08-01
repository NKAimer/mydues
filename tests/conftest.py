import pytest

from mydues import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A database in a throwaway MYDUES_HOME."""
    monkeypatch.setenv("MYDUES_HOME", str(tmp_path / "home"))
    # Do not pull the repo seed into unit-test DBs.
    monkeypatch.setenv("MYDUES_CATEGORIES_SEED", str(tmp_path / "no-seed.json"))
    connection = db.connect()
    db.init(connection)
    yield connection
    connection.close()
