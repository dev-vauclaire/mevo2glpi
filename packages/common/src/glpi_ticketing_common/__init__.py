"""Shared primitives for GLPI ticketing applications."""

from .release_identity import DeploymentEnvironment, ReleaseIdentity

__all__ = [
    "DeploymentEnvironment",
    "ReleaseIdentity",
]
