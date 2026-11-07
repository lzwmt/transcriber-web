# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    TRANSCRIBER_HOST=0.0.0.0 \
    TRANSCRIBER_PORT=8900 \
    TRANSCRIBER_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        gcc \
        libgomp1 \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

COPY app/ app/
COPY run.sh ./
RUN chmod +x run.sh

ENV UV_NO_SYNC=1

EXPOSE 8900

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import os,urllib.request; port=os.environ.get('TRANSCRIBER_PORT','8900'); r=urllib.request.urlopen('http://127.0.0.1:%s/health'%port, timeout=3); assert r.status==200"]

CMD ["./run.sh"]
