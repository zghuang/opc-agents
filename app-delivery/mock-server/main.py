"""
{PROJECT_NAME}-mocks — External API Mock Server

Runs standalone on port 8888, or as an embedded pytest fixture.

Usage:
  uvicorn main:app --port 8888 --reload    # standalone dev mode
  pytest                                   # auto-started as a session fixture

Adding a new mock:
  1. Create a new file in routers/ (see routers/example_api.py for reference)
  2. main.py auto-discovers all routers — no registration needed
  3. Add JSON fixtures to data/ if needed
"""

import importlib
import pathlib
import pkgutil

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="{PROJECT_NAME} Mock API",
    description="Mock implementations of external API dependencies for local dev and testing",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auto-discover and register all router modules under routers/
routers_path = pathlib.Path(__file__).parent / "routers"
if routers_path.exists():
    for module_info in pkgutil.iter_modules([str(routers_path)]):
        module = importlib.import_module(f"routers.{module_info.name}")
        if hasattr(module, "router"):
            prefix = f"/{module_info.name.replace('_api', '')}"
            app.include_router(module.router, prefix=prefix)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "{PROJECT_NAME}-mocks"}


@app.get("/")
def root() -> dict[str, object]:
    return {
        "message": "Mock API server running",
        "docs": "/docs",
        "available_routes": [r.path for r in app.routes],
    }
