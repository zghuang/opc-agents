FROM python:3.14-slim AS builder

WORKDIR /app
RUN pip install --no-cache-dir uv

COPY pyproject.toml .
RUN uv sync --no-dev

FROM python:3.14-slim AS runner

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

RUN useradd --system --no-create-home appuser
USER appuser

HEALTHCHECK --interval=30s --timeout=10s \
	CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"

CMD ["python", "-c", "print('Backend entrypoint is selected by architecture design')"]