import pytest

from src.main import health


@pytest.mark.asyncio
async def test_health_returns_ok() -> None:
    assert await health() == {"status": "ok"}