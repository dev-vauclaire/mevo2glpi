#!/bin/sh
set -eu

MINIMUM_ENGINE_VERSION=28.3.3
MINIMUM_COMPOSE_VERSION=2.24.0
DOCKER_BIN=/usr/bin/docker

if [ "$#" -ne 1 ]; then
    echo "usage: $0 /absolute/path/to/compose.env" >&2
    exit 64
fi

case "$1" in
    /*) ;;
    *)
        echo "compose environment path must be absolute" >&2
        exit 64
        ;;
esac

compose_environment=$1
if [ ! -r "$compose_environment" ]; then
    echo "compose environment is not readable" >&2
    exit 66
fi
if [ ! -x "$DOCKER_BIN" ]; then
    echo "docker CLI is not available at $DOCKER_BIN" >&2
    exit 69
fi
if ! command -v grep >/dev/null 2>&1; then
    echo "grep is required for configuration preflight" >&2
    exit 69
fi

version_is_at_least() {
    installed_version=$1
    minimum_version=$2
    oldest_version=$(
        printf '%s\n%s\n' "$installed_version" "$minimum_version" \
            | sort -V \
            | head -n 1
    )
    [ "$oldest_version" = "$minimum_version" ]
}

engine_version=$("$DOCKER_BIN" version --format '{{.Server.Version}}')
compose_version=$("$DOCKER_BIN" compose version --short)
compose_version=${compose_version#v}

if ! version_is_at_least "$engine_version" "$MINIMUM_ENGINE_VERSION"; then
    echo "Docker Engine $MINIMUM_ENGINE_VERSION or newer is required" >&2
    exit 69
fi
if ! version_is_at_least "$compose_version" "$MINIMUM_COMPOSE_VERSION"; then
    echo "Docker Compose $MINIMUM_COMPOSE_VERSION or newer is required" >&2
    exit 69
fi

compose() {
    "$DOCKER_BIN" compose --env-file "$compose_environment" "$@"
}

wait_for_existing_worker() {
    attempts=0
    while [ "$attempts" -lt 45 ]; do
        worker_state=$(
            "$DOCKER_BIN" inspect --format '{{.State.Status}}' "$worker_id"
        )
        worker_exit_code=$(
            "$DOCKER_BIN" inspect --format '{{.State.ExitCode}}' "$worker_id"
        )
        worker_health=$(
            "$DOCKER_BIN" inspect \
                --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
                "$worker_id"
        )

        case "$worker_state:$worker_health" in
            running:healthy)
                return 0
                ;;
            running:starting)
                attempts=$((attempts + 1))
                sleep 1
                ;;
            running:*)
                echo "refusing running worker with health=$worker_health" >&2
                return 1
                ;;
            *)
                echo "worker stopped while waiting for health: state=$worker_state exit=$worker_exit_code" >&2
                if [ "$worker_exit_code" -eq 0 ]; then
                    return 1
                fi
                return "$worker_exit_code"
                ;;
        esac
    done

    echo "worker healthcheck remained in starting state" >&2
    return 1
}

resolved_config=$(compose config --format json)
case "$resolved_config" in
    *replace-with-*)
        echo "runtime configuration still contains a placeholder" >&2
        exit 78
        ;;
esac
if printf '%s' "$resolved_config" \
    | grep -Eq '"VCS_REF"[[:space:]]*:[[:space:]]*"unknown"'; then
    echo "GTA_VCS_REF must identify the deployed commit" >&2
    exit 78
fi
unset resolved_config

compose up --detach --no-build --pull never \
    --wait --wait-timeout 45 web nginx

worker_id=$(compose ps --all --quiet worker)
if [ -n "$worker_id" ]; then
    worker_state=$("$DOCKER_BIN" inspect --format '{{.State.Status}}' "$worker_id")
    worker_exit_code=$(
        "$DOCKER_BIN" inspect --format '{{.State.ExitCode}}' "$worker_id"
    )

    case "$worker_state:$worker_exit_code" in
        running:*)
            worker_health_result=0
            wait_for_existing_worker || worker_health_result=$?
            exit "$worker_health_result"
            ;;
        created:*|exited:0)
            ;;
        *)
            echo "refusing to restart worker after state=$worker_state exit=$worker_exit_code" >&2
            if [ "$worker_exit_code" -eq 0 ]; then
                exit 1
            fi
            exit "$worker_exit_code"
            ;;
    esac
fi

compose up --detach --no-deps --no-build --pull never \
    --wait --wait-timeout 45 worker
