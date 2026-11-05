"""Shared primitives for GLPI ticketing applications."""

from .originating_phone_number import (
    OriginatingPhoneNumberCipher,
    OriginatingPhoneNumberCipherError,
    OriginatingPhoneNumberKeyError,
    format_originating_phone_number,
)
from .release_identity import DeploymentEnvironment, ReleaseIdentity

__all__ = [
    "DeploymentEnvironment",
    "OriginatingPhoneNumberCipher",
    "OriginatingPhoneNumberCipherError",
    "OriginatingPhoneNumberKeyError",
    "ReleaseIdentity",
    "format_originating_phone_number",
]
