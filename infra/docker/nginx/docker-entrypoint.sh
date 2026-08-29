#!/bin/sh
set -eu

external_https_port=${NGINX_EXTERNAL_HTTPS_PORT:-443}
public_host=${NGINX_PUBLIC_HOST:-localhost}

case "$public_host" in
    "" | .* | *. | *..* | *[!A-Za-z0-9.-]*)
        echo "NGINX_PUBLIC_HOST must be a DNS name or IPv4 address" >&2
        exit 64
        ;;
esac

if [ "${#public_host}" -gt 253 ]; then
    echo "NGINX_PUBLIC_HOST must be a DNS name or IPv4 address" >&2
    exit 64
fi

case "$external_https_port" in
    "" | 0 | 0* | *[!0-9]* | ??????*)
        echo "NGINX_EXTERNAL_HTTPS_PORT must be an integer between 1 and 65535" >&2
        exit 64
        ;;
esac

if [ "$external_https_port" -gt 65535 ]; then
    echo "NGINX_EXTERNAL_HTTPS_PORT must be an integer between 1 and 65535" >&2
    exit 64
fi

if [ "$external_https_port" -eq 443 ]; then
    NGINX_EXTERNAL_HTTPS_SUFFIX=""
else
    NGINX_EXTERNAL_HTTPS_SUFFIX=":$external_https_port"
fi
NGINX_PUBLIC_HOST=$public_host
export NGINX_EXTERNAL_HTTPS_SUFFIX
export NGINX_PUBLIC_HOST

envsubst '${NGINX_PUBLIC_HOST} ${NGINX_EXTERNAL_HTTPS_SUFFIX}' \
    < /etc/nginx/nginx.conf.template \
    > /tmp/nginx.conf

exec /usr/sbin/nginx -c /tmp/nginx.conf "$@"
