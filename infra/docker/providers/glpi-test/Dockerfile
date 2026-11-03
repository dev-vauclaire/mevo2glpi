# This image is exclusively used by local development and disposable tests.
# Keep both the immutable digest and the human-readable release tag: the tag
# documents the tested GLPI contract, while the digest makes builds reproducible.
FROM glpi/glpi:11.0.7@sha256:7e100562afe13cce132fe46c0a21ddb94f7ed2c06950da9c4b34ce734a1ad4ac

LABEL org.opencontainers.image.title="GLPI development and test image" \
      org.opencontainers.image.description="Local-only GLPI image; never deploy in production" \
      org.opencontainers.image.version="11.0.7-test"

USER root
RUN install --directory --mode=0750 --owner=www-data --group=www-data /opt/glpi-test
COPY --chown=www-data:www-data --chmod=0440 bootstrap_oauth.php /opt/glpi-test/bootstrap_oauth.php
COPY --chown=www-data:www-data --chmod=0440 bootstrap_request_source.php /opt/glpi-test/bootstrap_request_source.php
COPY --chown=www-data:www-data --chmod=0440 set_ticket_status.php /opt/glpi-test/set_ticket_status.php
USER www-data
