"""
pytest conftest — Lets main project tests use the mock server automatically.

Usage in the main project:
  Copy this file to the main project's root conftest.py (or import from it).

  Tests automatically start the mock server once per session and inject
  the mock URLs as environment variables — no test code changes needed.

  async def test_payment_flow(async_mock_client):
      response = await async_mock_client.post("/payments", json={...})
      assert response.status_code == 201
"""

import os
import sys
import threading
import time

import httpx
import pytest
import pytest_asyncio
import uvicorn

# ── Mock server configuration ────────────────────────────────────────────────

MOCK_SERVER_HOST = "127.0.0.1"
MOCK_SERVER_PORT = 8888
MOCK_BASE_URL = f"http://{MOCK_SERVER_HOST}:{MOCK_SERVER_PORT}"

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")


# ── Mock server fixture (session-scoped: started once for the whole test run) ─

@pytest.fixture(scope="session")
def mock_server():
    """Start the mock server once for the entire test session."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from main import app

    config = uvicorn.Config(app, host=MOCK_SERVER_HOST, port=MOCK_SERVER_PORT, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
    for _ in range(30):
        try:
            httpx.get(f"{MOCK_BASE_URL}/health", timeout=1.0)
            break
        except Exception:
            time.sleep(0.1)

    yield MOCK_BASE_URL

    server.should_exit = True


@pytest.fixture(scope="session")
def mock_base_url(mock_server: str) -> str:
    return mock_server


# ── HTTP client fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def mock_client(mock_server: str):
    """Synchronous httpx client pointing at the mock server."""
    with httpx.Client(base_url=mock_server, timeout=10.0) as client:
        yield client


@pytest_asyncio.fixture
async def async_mock_client(mock_server: str):
    """Async httpx client pointing at the mock server."""
    async with httpx.AsyncClient(base_url=mock_server, timeout=10.0) as client:
        yield client


@pytest_asyncio.fixture
async def api_client():
    """Async client pointing at the main project API (for integration tests)."""
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30.0) as client:
        yield client


# ── Environment variable injection ──────────────────────────────────────────

@pytest.fixture(autouse=True)
def inject_mock_urls(mock_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Automatically redirect all external API calls to the mock server.

    Adjust the environment variable names to match what your application reads.
    """
    monkeypatch.setenv("EXTERNAL_API_BASE_URL", mock_server)
    # Add service-specific vars as needed, e.g.:
    # monkeypatch.setenv("PAYMENT_API_URL", f"{mock_server}/payments")
    # monkeypatch.setenv("CRM_API_URL", f"{mock_server}/crm")
