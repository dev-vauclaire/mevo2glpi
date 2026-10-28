"""Contract tests for the shared deployment release identity."""

from __future__ import annotations

import pytest

from glpi_ticketing_common.release_identity import (
    DeploymentEnvironment,
    ReleaseIdentity,
)

COMMIT_SHA = "BD88D901F4569EA8EE455C53B20920B0E1140EE2"
NORMALIZED_COMMIT_SHA = COMMIT_SHA.lower()


def test_development_identity_has_safe_local_defaults() -> None:
    identity = ReleaseIdentity()

    assert identity.environment is DeploymentEnvironment.DEVELOPMENT
    assert identity.release == "development"
    assert identity.commit_sha == "unknown"
    assert identity.short_commit_sha == "unknown"


@pytest.mark.parametrize(
    ("environment", "release"),
    [
        (DeploymentEnvironment.DEVELOPMENT, "main"),
        (DeploymentEnvironment.DEVELOPMENT, "v0.2.0"),
        (DeploymentEnvironment.STAGING, "main"),
        (DeploymentEnvironment.STAGING, "v0.2.0"),
        (DeploymentEnvironment.PRODUCTION, "v0.2.0"),
        (DeploymentEnvironment.PRODUCTION, "v12.34.56"),
    ],
)
def test_deployed_identity_accepts_supported_release_references(
    environment: DeploymentEnvironment,
    release: str,
) -> None:
    identity = ReleaseIdentity(
        environment=environment,
        release=release,
        commit_sha=COMMIT_SHA,
    )

    assert identity.commit_sha == NORMALIZED_COMMIT_SHA
    assert identity.short_commit_sha == NORMALIZED_COMMIT_SHA[:12]


@pytest.mark.parametrize(
    "release",
    [
        "main",
        "development",
        "unknown",
        "CHANGE_ME",
        "v0.2",
        "v0.2.0-rc.1",
        "v00.2.0",
        "v0.02.0",
        "v0.2.00",
        "0.2.0",
    ],
)
def test_production_rejects_non_stable_semantic_release(release: str) -> None:
    with pytest.raises(ValueError):
        ReleaseIdentity(
            environment=DeploymentEnvironment.PRODUCTION,
            release=release,
            commit_sha=NORMALIZED_COMMIT_SHA,
        )


@pytest.mark.parametrize(
    "release",
    [
        "development",
        "unknown",
        "CHANGE_ME",
        "feature/example",
        "v0.2.0-rc.1",
    ],
)
def test_staging_rejects_unsupported_release_reference(release: str) -> None:
    with pytest.raises(ValueError):
        ReleaseIdentity(
            environment=DeploymentEnvironment.STAGING,
            release=release,
            commit_sha=NORMALIZED_COMMIT_SHA,
        )


@pytest.mark.parametrize(
    "commit_sha",
    [
        "unknown",
        "CHANGE_ME",
        "bd88d90",
        "g" * 40,
        "0" * 39,
        "0" * 41,
        "  " + ("0" * 40),
        ("0" * 40) + "\n",
    ],
)
def test_deployed_identity_requires_full_git_sha(commit_sha: str) -> None:
    with pytest.raises(ValueError):
        ReleaseIdentity(
            environment=DeploymentEnvironment.STAGING,
            release="main",
            commit_sha=commit_sha,
        )


@pytest.mark.parametrize(
    "environment",
    [
        "production",
        None,
        1,
    ],
)
def test_environment_must_be_typed(environment: object) -> None:
    with pytest.raises(TypeError):
        ReleaseIdentity(
            environment=environment,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("release", "commit_sha", "exception"),
    [
        (None, "unknown", TypeError),
        ("development", None, TypeError),
        (" development", "unknown", ValueError),
        ("development\n", "unknown", ValueError),
        ("x" * 65, "unknown", ValueError),
        ("development", "0" * 40, ValueError),
        ("main", "unknown", ValueError),
    ],
)
def test_development_identity_rejects_incoherent_or_invalid_values(
    release: object,
    commit_sha: object,
    exception: type[Exception],
) -> None:
    with pytest.raises(exception):
        ReleaseIdentity(
            release=release,  # type: ignore[arg-type]
            commit_sha=commit_sha,  # type: ignore[arg-type]
        )
