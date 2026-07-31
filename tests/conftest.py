import pytest

from carddues import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A database in a throwaway CARDDUES_HOME."""
    monkeypatch.setenv("CARDDUES_HOME", str(tmp_path / "home"))
    connection = db.connect()
    db.init(connection)
    yield connection
    connection.close()
