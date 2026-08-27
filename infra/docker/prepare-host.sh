#!/bin/sh
set -eu

APP_UID=10001
APP_GID=10001
NGINX_SECRET_GID=${GTA_NGINX_SECRET_GID:-10002}

if [ "$#" -ne 1 ]; then
    echo "usage: $0 /absolute/path/to/audio-root" >&2
    exit 64
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "prepare-host.sh must run as root" >&2
    exit 77
fi

if ! command -v getent >/dev/null 2>&1; then
    echo "getent is required to validate host UID/GID ownership" >&2
    exit 69
fi
if getent passwd "$APP_UID" >/dev/null; then
    echo "refusing host UID collision for $APP_UID" >&2
    exit 78
fi
if getent group "$APP_GID" >/dev/null; then
    echo "refusing host GID collision for $APP_GID" >&2
    exit 78
fi
case "$NGINX_SECRET_GID" in
    ""|*[!0-9]*)
        echo "GTA_NGINX_SECRET_GID must be a numeric GID" >&2
        exit 64
        ;;
esac
if [ "$NGINX_SECRET_GID" -eq "$APP_GID" ]; then
    echo "Nginx secret GID must differ from the audio GID" >&2
    exit 64
fi
if getent group "$NGINX_SECRET_GID" >/dev/null; then
    echo "refusing host GID collision for Nginx secrets: $NGINX_SECRET_GID" >&2
    exit 78
fi

case "$1" in
    /*) ;;
    *)
        echo "audio root must be an absolute path" >&2
        exit 64
        ;;
esac

audio_root=$(readlink -m -- "$1")
case "$audio_root" in
    /|/bin|/boot|/dev|/etc|/home|/opt|/proc|/root|/run|/srv|/sys|/tmp|/usr|/var)
        echo "refusing a broad system directory: $audio_root" >&2
        exit 64
        ;;
esac

install -d -m 0750 -o "$APP_UID" -g "$APP_GID" "$audio_root"
for lifecycle in pending processing completed failed; do
    install -d -m 0750 -o "$APP_UID" -g "$APP_GID" \
        "$audio_root/$lifecycle"
done

echo "audio directories prepared under $audio_root for $APP_UID:$APP_GID"
