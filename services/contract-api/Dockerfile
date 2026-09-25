FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations

FROM base AS runtime

RUN addgroup --system contractops && adduser --system --ingroup contractops contractops

RUN pip install .

USER contractops
EXPOSE 8080

CMD ["uvicorn", "contractops.main:app", "--host", "0.0.0.0", "--port", "8080"]

FROM base AS test

COPY tests ./tests
COPY scripts ./scripts

RUN pip install ".[dev]"

CMD ["python", "scripts/check.py"]
