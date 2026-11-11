# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.11@sha256:3472e43b4e738cf911c99d41bb34331280efad54c73b1def654a6227bb59b2b4

FROM ${UV_IMAGE} AS uv-binary

FROM ${PYTHON_IMAGE} AS builder

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

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
      --package mevo2glpi-worker-retention --no-install-workspace

COPY apps/worker-retention/src apps/worker-retention/src
COPY packages/common/src packages/common/src
COPY packages/database/src packages/database/src
COPY packages/glpi-client/src packages/glpi-client/src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
      --package mevo2glpi-worker-retention

FROM ${PYTHON_IMAGE} AS runtime

ARG IMAGE_VERSION
ARG VCS_REF

LABEL org.opencontainers.image.source="https://github.com/dev-vauclaire/mevo2glpi" \
      org.opencontainers.image.title="mevo2glpi audio-retention worker" \
      org.opencontainers.image.version="${IMAGE_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="EUPL-1.2"

RUN groupadd --gid 10001 mevo2glpi \
    && useradd --uid 10001 --gid 10001 \
      --no-create-home --no-log-init --home-dir /nonexistent \
      --shell /usr/sbin/nologin mevo2glpi \
    && install -d -m 0750 -o 10001 -g 10001 /data/audios

ENV APP_RELEASE=${IMAGE_VERSION} \
    APP_COMMIT_SHA=${VCS_REF} \
    HOME=/nonexistent \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp \
    AUDIO_ROOT=/data/audios \
    WORKER_HEALTHCHECK_STALE_AFTER_SECONDS=45

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --chown=10001:10001 --chmod=0444 LICENSE NOTICE /usr/share/licenses/mevo2glpi/

USER 10001:10001

STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 \
    --start-interval=2s \
    CMD ["python", "-m", "mevo2glpi_worker_retention.runtime.healthcheck"]

CMD ["mevo2glpi-worker-retention"]
