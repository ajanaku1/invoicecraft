import pytest


@pytest.fixture(autouse=True)
def isolate_db(monkeypatch, tmp_path):
    """Give each test its own SQLite DB so challenges, counters, and used-tx
    records don't leak between tests (the store reads DB_PATH at call time)."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    yield
