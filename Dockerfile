# birdloc - container image
# Base pinned by tag AND digest: python:3.12.7-slim, sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d
FROM python:3.12.7-slim@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d

COPY --from=ghcr.io/astral-sh/uv:0.5.8 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# dependency layer first: unchanged lockfile means an unchanged layer
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY bands.yaml README.md ./
COPY birdloc ./birdloc
RUN uv sync --frozen --no-dev

# The service writes only /data (a bind mount owned by the host user) and reads
# /map read-only; nothing needs root. Numeric so it matches the host uid on the
# bind mount without a passwd entry.
USER 1000:1000

EXPOSE 8000
# No curl/wget in slim; the interpreter is already here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "birdloc.web:app", "--host", "0.0.0.0", "--port", "8000"]
