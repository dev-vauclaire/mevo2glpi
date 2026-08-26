# This image is exclusively used by the disposable GLPI integration tests.
# Keep both the immutable digest and the human-readable release tag: the tag
# documents the tested GLPI contract, while the digest makes builds reproducible.
FROM glpi/glpi:11.0.8@sha256:ca6b53ef948e940f41e1f4af31c001097897e819b66d146d3e5cac51ac7e9f14

LABEL org.opencontainers.image.title="GLPI integration-test image" \
      org.opencontainers.image.description="Disposable GLPI image; never deploy in production" \
      org.opencontainers.image.version="11.0.8-test"

USER root
RUN install --directory --mode=0750 --owner=www-data --group=www-data /opt/glpi-test
COPY --chown=www-data:www-data --chmod=0440 bootstrap_oauth.php /opt/glpi-test/bootstrap_oauth.php
USER www-data
