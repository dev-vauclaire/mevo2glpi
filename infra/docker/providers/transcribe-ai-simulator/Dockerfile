# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

# Tags keep upgrades readable; digests make builds reproducible and multi-arch.
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.11@sha256:3472e43b4e738cf911c99d41bb34331280efad54c73b1def654a6227bb59b2b4

FROM ${UV_IMAGE} AS uv-binary

FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app/providers

COPY --from=uv-binary /uv /usr/local/bin/uv
COPY providers/pyproject.toml providers/uv.lock ./
COPY providers/simulators/microsoft-graph-simulator/pyproject.toml simulators/microsoft-graph-simulator/pyproject.toml
COPY providers/simulators/transcribe-ai-simulator/pyproject.toml simulators/transcribe-ai-simulator/pyproject.toml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
      --package glpi-ticketing-transcribe-ai-simulator --no-install-workspace

COPY providers/simulators/transcribe-ai-simulator/src simulators/transcribe-ai-simulator/src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
      --package glpi-ticketing-transcribe-ai-simulator

FROM ${PYTHON_IMAGE} AS runtime

ARG IMAGE_VERSION=0.1.0-staging
ARG VCS_REF=unknown

LABEL org.opencontainers.image.source="https://github.com/dev-vauclaire/glpi-ticketing-automation" \
      org.opencontainers.image.title="Transcribe AI staging simulator" \
      org.opencontainers.image.version="${IMAGE_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

RUN groupadd --gid 10001 gta \
    && useradd --uid 10001 --gid 10001 \
      --no-create-home --no-log-init --home-dir /nonexistent \
      --shell /usr/sbin/nologin gta

ENV HOME=/nonexistent \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp \
    TRANSCRIBE_AI_SIMULATOR_HOST=0.0.0.0 \
    TRANSCRIBE_AI_SIMULATOR_PORT=8080

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER 10001:10001

EXPOSE 8080/tcp
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=3s --start-period=3s --retries=3 \
    --start-interval=1s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).close()"]

CMD ["glpi-ticketing-transcribe-ai-simulator"]
