# This image is reserved for the isolated staging providers environment.
# Keep the release tag for readability and the manifest-list digest for
# reproducible builds on both linux/amd64 and linux/arm64 hosts.
FROM glpi/glpi:11.0.7@sha256:7e100562afe13cce132fe46c0a21ddb94f7ed2c06950da9c4b34ce734a1ad4ac

LABEL org.opencontainers.image.title="GLPI staging providers image" \
      org.opencontainers.image.description="GLPI 11.0.7 with an idempotent staging-only OAuth bootstrap" \
      org.opencontainers.image.version="11.0.7-staging"

USER root
RUN install --directory --mode=0750 --owner=www-data --group=www-data /opt/glpi-staging
COPY --chown=www-data:www-data --chmod=0440 bootstrap_oauth.php /opt/glpi-staging/bootstrap_oauth.php
USER www-data
