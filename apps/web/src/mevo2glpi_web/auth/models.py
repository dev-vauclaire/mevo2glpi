"""Modeles internes communs aux modes d'authentification web."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

ACCESS_ROLE = "mevo2glpi-access"


class AuthMode(StrEnum):
    """Modes d'authentification mutuellement exclusifs."""

    LOCAL = "local"
    OIDC = "oidc"


@dataclass(frozen=True, slots=True)
class Principal:
    """Identite minimale autorisee a circuler dans l'application.

    ``subject`` est soit l'UUID d'un compte local, soit un identifiant OIDC
    pseudonymise. Il ne doit jamais contenir de nom, d'adresse electronique ou
    de jeton fourni par un fournisseur d'identite.
    """

    auth_mode: AuthMode
    subject: str
    roles: frozenset[str]
    session_version: int | None = None

    def __post_init__(self) -> None:
        if not self.subject or len(self.subject) > 256:
            raise ValueError("invalid principal subject")
        if any(
            character.isspace() or ord(character) < 32 for character in self.subject
        ):
            raise ValueError("invalid principal subject")
        if not self.roles or any(not role or len(role) > 128 for role in self.roles):
            raise ValueError("invalid principal roles")
        if self.auth_mode is AuthMode.LOCAL:
            if isinstance(self.session_version, bool) or not isinstance(
                self.session_version, int
            ):
                raise ValueError("local principals require a session version")
            if self.session_version < 0:
                raise ValueError("invalid local session version")
        elif self.session_version is not None:
            raise ValueError("OIDC principals cannot carry a local session version")

    def has_role(self, role: str) -> bool:
        return role in self.roles


@dataclass(frozen=True, slots=True)
class SessionPayload:
    """Contenu minimal signe dans le cookie de session.

    Aucun jeton OIDC, mot de passe, nom d'utilisateur ou autre donnee
    personnelle ne doit etre ajoute a ce modele.
    """

    principal: Principal
    issued_at: datetime
    expires_at: datetime
    csrf_token: str

    def __post_init__(self) -> None:
        for timestamp in (self.issued_at, self.expires_at):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("session timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("session expiration must follow its issue date")
        if self.issued_at.utcoffset() != UTC.utcoffset(self.issued_at):
            object.__setattr__(self, "issued_at", self.issued_at.astimezone(UTC))
        if self.expires_at.utcoffset() != UTC.utcoffset(self.expires_at):
            object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))
        if not self.csrf_token or len(self.csrf_token) > 128:
            raise ValueError("invalid CSRF token")
