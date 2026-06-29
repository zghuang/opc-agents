import pytest
from httpx import ASGITransport, AsyncClient

from src.main import app, database_health, health


@pytest.mark.asyncio
async def test_health_returns_ok() -> None:
    assert await health() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_database_health_uses_async_check(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def fake_check_db() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("src.main.check_db", fake_check_db)

    assert await database_health() == {"status": "ok"}
    assert called is True