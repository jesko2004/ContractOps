FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
COPY scripts ./scripts

FROM base AS runtime

RUN addgroup --system contractops && adduser --system --ingroup contractops contractops

RUN uv sync --frozen --no-dev --group build --no-install-project \
    && uv sync --frozen --no-dev --group build --no-build-isolation --no-editable

USER contractops
EXPOSE 8080

CMD ["uvicorn", "contractops.main:app", "--host", "0.0.0.0", "--port", "8080"]

FROM base AS test

COPY tests ./tests
COPY scripts ./scripts

RUN uv sync --frozen --extra dev --group build --no-install-project \
    && uv sync --frozen --extra dev --group build --no-build-isolation --no-editable

CMD ["python", "scripts/check.py"]
