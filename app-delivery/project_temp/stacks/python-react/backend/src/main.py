from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.config import settings
from .core.database import close_db, init_db
from .core.registry import iter_routers, iter_seeders


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    seeders = iter_seeders()
    if seeders and not settings.is_production:
        from .core.database import SessionLocal

        async with SessionLocal() as session:
            for seed_data in seeders:
                await seed_data(session)
            await session.commit()
    yield
    await close_db()


# Auth/current-user wiring for this FastAPI template should use dependencies
# (router-level or app-level Depends) that return the current user/tenant/role.
# Do not add Starlette BaseHTTPMiddleware that mutates request.state for auth;
# state propagation through BaseHTTPMiddleware is brittle across Starlette
# versions and can pass patched router tests while failing in the real app. If
# cross-cutting auth middleware is unavoidable, use @app.middleware("http") and
# cover it through integration tests against this actual FastAPI app.
app = FastAPI(
    title="{{PROJECT_NAME}} API",
    lifespan=lifespan,
    docs_url="/api/docs" if not settings.is_production else None,
)

app.add_middleware(
    CORSMiddleware,
    # Allow both localhost and 127.0.0.1 dev origins. opc-runtime.py and the
    # Playwright harness standardize on 127.0.0.1, while browsers/users often use
    # localhost; a single-origin allow-list silently breaks browser fetches with
    # a CORS error that unit tests and build never catch.
    allow_origins=(
        ["http://localhost:5173", "http://127.0.0.1:5173"]
        if not settings.is_production
        else []
    ),
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Feature-module routers are discovered and included automatically. Adding a
# module means dropping src/<module>/router.py with a `router` object; no edit
# to this file is needed (see core/registry.py).
for _router in iter_routers():
    app.include_router(_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}