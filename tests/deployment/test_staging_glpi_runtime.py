"""Exercise the persistent GLPI staging image and deterministic OAuth bootstrap."""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from .conftest import (
    DOCKER_ROOT,
    REPOSITORY_ROOT,
    STAGING_COMPOSE_FILE,
    clean_environment,
    run_command,
)

pytestmark = [
    pytest.mark.deployment,
    pytest.mark.glpi,
    pytest.mark.timeout(15 * 60),
]

_TOKEN_PROBE = r"""
$body = http_build_query([
    'grant_type' => 'password',
    'client_id' => getenv('GLPI_CLIENT_ID'),
    'client_secret' => getenv('GLPI_CLIENT_SECRET'),
    'username' => getenv('GLPI_USERNAME'),
    'password' => getenv('GLPI_PASSWORD'),
    'scope' => 'api',
]);
$context = stream_context_create(['http' => [
    'method' => 'POST',
    'header' => "Accept: application/json\r\nContent-Type: application/x-www-form-urlencoded\r\n",
    'content' => $body,
    'ignore_errors' => true,
    'timeout' => 10,
]]);
$response = @file_get_contents(
    'http://glpi/api.php/v2.3.0/token',
    false,
    $context
);
$status = $http_response_header[0] ?? '';
if (!str_contains($status, ' 200 ')) {
    exit(1);
}
$payload = json_decode((string) $response, true);
exit(is_array($payload) && !empty($payload['access_token']) ? 0 : 1);
"""

_SECURITY_PROBE = r"""
$glpiRoot = '/var/www/glpi';
chdir($glpiRoot);
require_once $glpiRoot . '/src/Glpi/Application/ResourcesChecker.php';
(new Glpi\Application\ResourcesChecker($glpiRoot))->checkResources();
require_once $glpiRoot . '/vendor/autoload.php';
$kernel = new Glpi\Kernel\Kernel();
$kernel->boot();

$admin = new User();
$profile = new Profile();
$authorization = new Profile_User();
if (
    !$admin->getFromDBByCrit(['name' => getenv('GLPI_ADMIN_USERNAME')])
    || !$profile->getFromDBByCrit(['name' => 'Super-Admin'])
    || !$authorization->getFromDBByCrit([
        'users_id' => (int) $admin->getID(),
        'profiles_id' => (int) $profile->getID(),
        'entities_id' => 0,
    ])
) {
    exit(1);
}

$defaultUsersInactive = true;
foreach (['glpi', 'tech', 'normal', 'post-only'] as $defaultUsername) {
    $defaultUser = new User();
    if (
        !$defaultUser->getFromDBByCrit(['name' => $defaultUsername])
        || (int) ($defaultUser->fields['is_active'] ?? 1) !== 0
    ) {
        $defaultUsersInactive = false;
    }
}

echo json_encode([
    'admin_active' => (int) ($admin->fields['is_active'] ?? 0) === 1,
    'super_admin_recursive' => (
        (int) ($authorization->fields['is_recursive'] ?? 0) === 1
    ),
    'default_users_inactive' => $defaultUsersInactive,
], JSON_THROW_ON_ERROR);
"""


@dataclass(slots=True)
class StagingGlpiEnvironment:
    """Own one isolated GLPI/MySQL staging pair."""

    project_name: str
    environment_file: Path
    process_environment: Mapping[str, str] = field(repr=False)
    network_names: tuple[str, str]

    def compose(
        self,
        *arguments: str,
        check: bool = True,
        timeout_seconds: float = 600,
    ):
        return run_command(
            [
                "docker",
                "compose",
                "--project-directory",
                str(REPOSITORY_ROOT),
                "--project-name",
                self.project_name,
                "--env-file",
                str(self.environment_file),
                "--file",
                str(STAGING_COMPOSE_FILE),
                *arguments,
            ],
            environment=self.process_environment,
            check=check,
            timeout_seconds=timeout_seconds,
        )


def _read_example_environment() -> dict[str, str]:
    source = (DOCKER_ROOT / "staging.env.example").read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", source, re.MULTILINE))


def _write_environment(path: Path, values: Mapping[str, str]) -> None:
    path.write_text(
        "".join(f"{name}={value}\n" for name, value in sorted(values.items())),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _docker_network(command: str, network_name: str, *, check: bool = True) -> None:
    run_command(
        ["docker", "network", command, network_name],
        environment=clean_environment(),
        check=check,
    )


@pytest.fixture(scope="session")
def staging_glpi_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[StagingGlpiEnvironment]:
    """Build and remove one exact, disposable copy of staging GLPI."""
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        pytest.fail("The staging GLPI suite must not run with pytest-xdist")

    suffix = secrets.token_hex(6)
    project_name = f"gta-staging-test-{suffix}"
    database_network = f"{project_name}-database"
    dokploy_network = f"{project_name}-dokploy"
    runtime_root = tmp_path_factory.mktemp("gta-staging-glpi")
    environment_file = runtime_root / "compose.env"
    values = _read_example_environment()
    values.update(
        {
            "COMPOSE_PROJECT_NAME": project_name,
            "DATABASE_NETWORK_NAME": database_network,
            "DOKPLOY_NETWORK_NAME": dokploy_network,
            "AUDIO_VOLUME_NAME": f"{project_name}-audio",
            "GLPI_DATA_VOLUME_NAME": f"{project_name}-glpi-data",
            "GLPI_DATABASE_VOLUME_NAME": f"{project_name}-mysql-data",
            "GLPI_DB_PASSWORD": secrets.token_urlsafe(32),
            "GLPI_DB_ROOT_PASSWORD": secrets.token_urlsafe(32),
            "GLPI_ADMIN_USERNAME": f"glpi-staging-admin-{suffix}",
            "GLPI_ADMIN_PASSWORD": secrets.token_urlsafe(32),
            "GLPI_USERNAME": f"glpi-staging-worker-{suffix}",
            "GLPI_PASSWORD": secrets.token_urlsafe(32),
            "GLPI_OAUTH_CLIENT_NAME": f"glpi-staging-oauth-{suffix}",
            "GLPI_CLIENT_ID": f"glpi-staging-client-{suffix}",
            "GLPI_CLIENT_SECRET": secrets.token_urlsafe(32),
            "PROVIDER_SIMULATOR_ADMIN_TOKEN": secrets.token_urlsafe(32),
            "PROVIDER_SIMULATOR_TRANSCRIPTION_API_KEY": secrets.token_urlsafe(32),
        }
    )
    _write_environment(environment_file, values)
    process_environment = {
        **clean_environment(),
        "GTA_DOKPLOY_ENV_FILE": str(environment_file),
    }
    environment = StagingGlpiEnvironment(
        project_name=project_name,
        environment_file=environment_file,
        process_environment=process_environment,
        network_names=(database_network, dokploy_network),
    )
    primary_error: BaseException | None = None

    try:
        for network_name in environment.network_names:
            _docker_network("create", network_name)
        environment.compose("build", "glpi")
        environment.compose("up", "--detach", "glpi")
        yield environment
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_errors: list[Exception] = []
        try:
            environment.compose(
                "down",
                "--volumes",
                "--remove-orphans",
                check=False,
            )
        except Exception as error:
            cleanup_errors.append(error)
        for network_name in reversed(environment.network_names):
            try:
                _docker_network("rm", network_name, check=False)
            except Exception as error:
                cleanup_errors.append(error)
        run_command(
            ["docker", "image", "rm", f"{project_name}-glpi:11.0.7"],
            environment=clean_environment(),
            check=False,
        )
        if cleanup_errors:
            if primary_error is None:
                raise RuntimeError("Staging GLPI cleanup failed") from cleanup_errors[0]
            primary_error.add_note("Staging GLPI cleanup also encountered an error")


def _bootstrap(environment: StagingGlpiEnvironment) -> dict[str, object]:
    result = environment.compose("run", "--rm", "glpi-bootstrap")
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def test_staging_glpi_bootstrap_is_idempotent_and_issues_a_token(
    staging_glpi_environment: StagingGlpiEnvironment,
) -> None:
    first = _bootstrap(staging_glpi_environment)
    second = _bootstrap(staging_glpi_environment)
    version = staging_glpi_environment.compose(
        "exec",
        "--no-TTY",
        "glpi",
        "php",
        "bin/console",
        "--version",
        "--no-ansi",
    )
    token_probe = staging_glpi_environment.compose(
        "run",
        "--rm",
        "--entrypoint",
        "php",
        "glpi-bootstrap",
        "-r",
        _TOKEN_PROBE,
    )
    security_probe = staging_glpi_environment.compose(
        "run",
        "--rm",
        "--entrypoint",
        "php",
        "glpi-bootstrap",
        "-r",
        _SECURITY_PROBE,
    )
    security_state = json.loads(security_probe.stdout)

    assert first == second
    assert first["status"] == "ready"
    assert "11.0.7" in version.stdout
    assert token_probe.returncode == 0
    assert security_state == {
        "admin_active": True,
        "super_admin_recursive": True,
        "default_users_inactive": True,
    }


def test_staging_glpi_bootstrap_rejects_implicit_secret_rotation(
    staging_glpi_environment: StagingGlpiEnvironment,
) -> None:
    _bootstrap(staging_glpi_environment)
    divergent_secret = secrets.token_urlsafe(40)
    rejected_rotation = staging_glpi_environment.compose(
        "run",
        "--rm",
        "--env",
        f"GLPI_PASSWORD={divergent_secret}",
        "glpi-bootstrap",
        check=False,
    )
    restored = _bootstrap(staging_glpi_environment)
    token_probe = staging_glpi_environment.compose(
        "run",
        "--rm",
        "--entrypoint",
        "php",
        "glpi-bootstrap",
        "-r",
        _TOKEN_PROBE,
    )

    assert rejected_rotation.returncode != 0
    assert divergent_secret not in rejected_rotation.stdout
    assert divergent_secret not in rejected_rotation.stderr
    assert restored["status"] == "ready"
    assert token_probe.returncode == 0
