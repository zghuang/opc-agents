from collections.abc import Callable
from typing import Any

import pytest

from src.core import database
from src.core.config import Settings


class FakeConnection:
    def __init__(self) -> None:
        self.ran_sync = False
        self.sync_callback: Callable[[Any], Any] | None = None

    async def run_sync(self, callback: Callable[[Any], Any]) -> None:
        # init_db runs Base.metadata.create_all via run_sync. Capture the
        # callback rather than executing DDL against a fake bind; the assertion
        # below proves init_db drives create_all.
        self.ran_sync = True
        self.sync_callback = callback


class FakeBeginContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        return None


class FakeEngine:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.disposed = False

    def begin(self) -> FakeBeginContext:
        return FakeBeginContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


class FakeSession:
    def __init__(self) -> None:
        self.entered = False
        self.closed = False
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> "FakeSession":
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        self.closed = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class FakeSessionFactory:
    def __init__(self) -> None:
        self.session = FakeSession()

    def __call__(self) -> FakeSession:
        return self.session


def test_settings_is_production_property() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://postgres:test@localhost/test_db",
        secret_key="test-secret-key",
        environment="production",
    )

    assert settings.is_production is True


@pytest.mark.asyncio
async def test_init_and_close_db_use_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = FakeEngine()
    monkeypatch.setattr(database, "engine", fake_engine)

    await database.init_db()
    await database.close_db()

    assert fake_engine.connection.ran_sync is True
    assert fake_engine.connection.sync_callback == database.Base.metadata.create_all
    assert fake_engine.disposed is True


@pytest.mark.asyncio
async def test_get_session_yields_factory_session(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_factory = FakeSessionFactory()
    monkeypatch.setattr(database, "SessionLocal", fake_factory)

    yielded_sessions: list[FakeSession] = []
    async for session in database.get_session():
        yielded_sessions.append(session)

    assert yielded_sessions == [fake_factory.session]
    assert fake_factory.session.entered is True
    assert fake_factory.session.closed is True
    assert fake_factory.session.committed is True
    assert fake_factory.session.rolled_back is False


@pytest.mark.asyncio
async def test_get_session_rolls_back_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_factory = FakeSessionFactory()
    monkeypatch.setattr(database, "SessionLocal", fake_factory)

    generator = database.get_session()
    yielded_session = await anext(generator)

    assert yielded_session is fake_factory.session

    with pytest.raises(RuntimeError):
        await generator.athrow(RuntimeError("request failed"))

    assert fake_factory.session.closed is True
    assert fake_factory.session.committed is False
    assert fake_factory.session.rolled_back is True