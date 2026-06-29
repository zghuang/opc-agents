import pytest

from src.runtime import database


class FakeConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def fetchval(self, query: str) -> int:
        self.queries.append(query)
        return 1


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.closed = False

    def acquire(self) -> FakeConnection:
        return self.connection

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_check_db_uses_async_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pool = FakePool()
    monkeypatch.setattr(database, "pool", fake_pool)

    await database.check_db()

    assert fake_pool.connection.queries == ["select 1"]


@pytest.mark.asyncio
async def test_close_db_closes_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pool = FakePool()
    monkeypatch.setattr(database, "pool", fake_pool)

    await database.close_db()

    assert fake_pool.closed is True
    assert database.pool is None