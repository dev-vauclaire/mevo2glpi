"""Validated, non-sensitive identity of one deployed application revision."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

DEVELOPMENT_RELEASE = "development"
UNKNOWN_COMMIT_SHA = "unknown"
STAGING_BRANCH_RELEASE = "main"
SHORT_COMMIT_SHA_LENGTH = 12
MAX_RELEASE_LENGTH = 64

_SEMANTIC_RELEASE_PATTERN = re.compile(
    r"v(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
)
_FULL_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}")


class DeploymentEnvironment(StrEnum):
    """Stable environments exposed by the release identity contract."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    """Describe the Git revision expected to run in one environment."""

    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT
    release: str = DEVELOPMENT_RELEASE
    commit_sha: str = UNKNOWN_COMMIT_SHA

    def __post_init__(self) -> None:
        if not isinstance(self.environment, DeploymentEnvironment):
            raise TypeError("environment must be a DeploymentEnvironment")
        _validate_text(self.release, field_name="release", maximum=MAX_RELEASE_LENGTH)
        _validate_text(
            self.commit_sha,
            field_name="commit_sha",
            maximum=40,
        )

        if self.environment is DeploymentEnvironment.DEVELOPMENT:
            _validate_development_identity(self.release, self.commit_sha)
        elif self.environment is DeploymentEnvironment.STAGING:
            _validate_staging_identity(self.release, self.commit_sha)
        else:
            _validate_production_identity(self.release, self.commit_sha)

        if _FULL_COMMIT_SHA_PATTERN.fullmatch(self.commit_sha):
            object.__setattr__(self, "commit_sha", self.commit_sha.lower())

    @property
    def short_commit_sha(self) -> str:
        """Return a compact SHA for human-facing interfaces."""

        if self.commit_sha == UNKNOWN_COMMIT_SHA:
            return UNKNOWN_COMMIT_SHA
        return self.commit_sha[:SHORT_COMMIT_SHA_LENGTH]


def _validate_text(value: object, *, field_name: str, maximum: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > maximum
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(f"{field_name} is invalid")


def _is_semantic_release(value: str) -> bool:
    return _SEMANTIC_RELEASE_PATTERN.fullmatch(value) is not None


def _require_full_commit_sha(value: str) -> None:
    if _FULL_COMMIT_SHA_PATTERN.fullmatch(value) is None:
        raise ValueError("commit_sha must be a full Git SHA")


def _validate_development_identity(release: str, commit_sha: str) -> None:
    if release == DEVELOPMENT_RELEASE and commit_sha == UNKNOWN_COMMIT_SHA:
        return
    if release != STAGING_BRANCH_RELEASE and not _is_semantic_release(release):
        raise ValueError("development release is invalid")
    _require_full_commit_sha(commit_sha)


def _validate_staging_identity(release: str, commit_sha: str) -> None:
    if release != STAGING_BRANCH_RELEASE and not _is_semantic_release(release):
        raise ValueError("staging release is invalid")
    _require_full_commit_sha(commit_sha)


def _validate_production_identity(release: str, commit_sha: str) -> None:
    if not _is_semantic_release(release):
        raise ValueError("production release must be a stable semantic tag")
    _require_full_commit_sha(commit_sha)


__all__ = [
    "DEVELOPMENT_RELEASE",
    "STAGING_BRANCH_RELEASE",
    "UNKNOWN_COMMIT_SHA",
    "DeploymentEnvironment",
    "ReleaseIdentity",
]
