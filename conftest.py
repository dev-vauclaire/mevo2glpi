"""Pytest plugins shared by the workspace test suites."""

pytest_plugins = (
    "tests.integration.glpi.plugin",
    "tests.integration.postgres.plugin",
)
