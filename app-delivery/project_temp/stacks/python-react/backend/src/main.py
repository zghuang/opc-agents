from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .runtime.database import check_db, close_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await close_db()


app = FastAPI(
    title="{{PROJECT_NAME}} API",
    lifespan=lifespan,
    docs_url="/api/docs",
)

app.add_middleware(
    CORSMiddleware,
    # Allow both localhost and 127.0.0.1 dev origins. opc-runtime.py and the
    # Playwright harness standardize on 127.0.0.1, while browsers/users often use
    # localhost; a single-origin allow-list silently breaks browser fetches with
    # a CORS error that unit tests and build never catch.
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/db")
async def database_health() -> dict[str, str]:
    await check_db()
    return {"status": "ok"}