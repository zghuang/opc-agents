# Python React Stack Contract

- Backend API uses FastAPI.
- The template does not prescribe a backend package directory.
- Choose the backend package layout during architecture design from requirements, domain boundaries, framework conventions, and implementation ergonomics.
- Use a specific backend package layout only when the source requirements or an explicit architecture decision selects it.
- Put the FastAPI app entrypoint, runtime configuration, and database helpers wherever the selected backend layout makes them coherent; record that layout in `architecture_md` Module Architecture.
- Use async FastAPI handlers for I/O-bound API paths.
- Use async database access for runtime database calls; the default template uses `asyncpg`.
- Manage long-lived async resources, such as database pools or external clients, through explicit FastAPI application lifecycle setup and cleanup when the selected implementation needs them.
- Backend tests should follow the architecture-selected backend layout; common choices include `backend/tests/...` or package-local tests.
- Frontend implementation code lives under `frontend/src/...`.
- Browser/e2e tests live under `frontend/e2e/...`.
- Prefer project-root `mock-server/...` for simulated tool services and mock-only fixtures unless requirements select a different mock-service layout.
- Production MCP/FastMCP code belongs in the architecture-selected backend implementation layout unless requirements explicitly select a separate service root.