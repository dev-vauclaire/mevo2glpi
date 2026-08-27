# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM nginx:1.30.4-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46

ARG IMAGE_VERSION=0.1.0-poc
ARG VCS_REF=unknown

LABEL org.opencontainers.image.source="https://github.com/dev-vauclaire/glpi-ticketing-automation" \
      org.opencontainers.image.title="GLPI ticketing reverse proxy" \
      org.opencontainers.image.version="${IMAGE_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 8443/tcp
STOPSIGNAL SIGQUIT
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    --start-interval=1s \
    CMD ["wget", "--quiet", "--output-document=/dev/null", "http://127.0.0.1:8081/healthz"]

USER 101:101

ENTRYPOINT ["/usr/sbin/nginx"]
CMD ["-g", "daemon off;"]
