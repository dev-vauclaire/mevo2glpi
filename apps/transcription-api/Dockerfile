# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.11@sha256:3472e43b4e738cf911c99d41bb34331280efad54c73b1def654a6227bb59b2b4
ARG TRANSCRIBE_CPP_VERSION=0.4.1
ARG PARAKEET_ULTRA_MODEL_URL=https://huggingface.co/handy-computer/parakeet-ultra-gguf/resolve/a1539be1b849ea7463b88343e5277b373d0c4ea9/parakeet-ultra-Q8_0.gguf
ARG PARAKEET_ULTRA_MODEL_SHA256=007a59761e9258779f189df396b50d12b83bb5b79e66df4b955c230d2f2a0a59

FROM ${UV_IMAGE} AS uv-binary

FROM ${PYTHON_IMAGE} AS model-fetcher

ARG PARAKEET_ULTRA_MODEL_URL
ARG PARAKEET_ULTRA_MODEL_SHA256

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0755 /models \
    && curl --fail --show-error --location --retry 3 \
      --output /models/parakeet-ultra-Q8_0.gguf \
      "${PARAKEET_ULTRA_MODEL_URL}" \
    && echo "${PARAKEET_ULTRA_MODEL_SHA256}  /models/parakeet-ultra-Q8_0.gguf" \
      | sha256sum --check --strict \
    && chmod 0444 /models/parakeet-ultra-Q8_0.gguf

FROM ${PYTHON_IMAGE} AS builder

ARG TRANSCRIBE_CPP_VERSION

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY --from=uv-binary /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY apps/database-migrations/pyproject.toml apps/database-migrations/pyproject.toml
COPY apps/transcription-api/pyproject.toml apps/transcription-api/pyproject.toml
COPY apps/web/pyproject.toml apps/web/pyproject.toml
COPY apps/worker-automation/pyproject.toml apps/worker-automation/pyproject.toml
COPY apps/worker-publisher/pyproject.toml apps/worker-publisher/pyproject.toml
COPY apps/worker-retention/pyproject.toml apps/worker-retention/pyproject.toml
COPY packages/common/pyproject.toml packages/common/pyproject.toml
COPY packages/database/pyproject.toml packages/database/pyproject.toml
COPY packages/glpi-client/pyproject.toml packages/glpi-client/pyproject.toml
COPY providers/simulators/microsoft-graph-simulator/pyproject.toml providers/simulators/microsoft-graph-simulator/pyproject.toml
COPY providers/simulators/openai-transcription-simulator/pyproject.toml providers/simulators/openai-transcription-simulator/pyproject.toml
COPY providers/simulators/transcribe-ai-simulator/pyproject.toml providers/simulators/transcribe-ai-simulator/pyproject.toml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
      --package glpi-ticketing-transcription-api --no-install-workspace

COPY apps/transcription-api/src apps/transcription-api/src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
      --package glpi-ticketing-transcription-api \
    && test "$(/app/.venv/bin/python -c 'import transcribe_cpp; print(transcribe_cpp.__version__)')" = "${TRANSCRIBE_CPP_VERSION}"

FROM ${PYTHON_IMAGE} AS runtime

ARG IMAGE_VERSION
ARG VCS_REF
ARG TRANSCRIBE_CPP_VERSION

LABEL org.opencontainers.image.source="https://github.com/dev-vauclaire/glpi-ticketing-automation" \
      org.opencontainers.image.title="GLPI ticketing transcription API" \
      org.opencontainers.image.version="${IMAGE_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="EUPL-1.2 AND MIT AND CC-BY-4.0" \
      org.opencontainers.image.transcribe-cpp.version="${TRANSCRIBE_CPP_VERSION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 gta \
    && useradd --uid 10001 --gid 10001 \
      --no-create-home --no-log-init --home-dir /nonexistent \
      --shell /usr/sbin/nologin gta \
    && install -d -m 0755 -o 10001 -g 10001 /models

ENV APP_RELEASE=${IMAGE_VERSION} \
    APP_COMMIT_SHA=${VCS_REF} \
    HOME=/nonexistent \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp \
    TRANSCRIBE_BACKEND=cpu \
    TRANSCRIPTION_HOST=0.0.0.0 \
    TRANSCRIPTION_PORT=5092 \
    TRANSCRIPTION_MODEL_PATH=/models/parakeet-ultra-Q8_0.gguf \
    TRANSCRIPTION_MODEL_NAME=parakeet-ultra

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=model-fetcher --chown=10001:10001 /models /models
COPY --chown=10001:10001 apps/transcription-api/THIRD_PARTY_NOTICES.md /app/THIRD_PARTY_NOTICES.md
COPY --chown=10001:10001 --chmod=0444 LICENSE NOTICE /usr/share/licenses/glpi-ticketing-automation/

USER 10001:10001

EXPOSE 5092/tcp
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=3s --start-period=90s --retries=3 \
    --start-interval=2s \
    CMD ["python", "-c", "import os, urllib.request; port = int(os.environ.get('TRANSCRIPTION_PORT', '5092')); urllib.request.urlopen(f'http://127.0.0.1:{port}/health/ready', timeout=2).close()"]

CMD ["glpi-ticketing-transcription-api"]
