FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system contractops && adduser --system --ingroup contractops contractops

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install .

USER contractops
EXPOSE 8080

CMD ["uvicorn", "contractops.main:app", "--host", "0.0.0.0", "--port", "8080"]
