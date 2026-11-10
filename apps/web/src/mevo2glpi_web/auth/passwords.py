"""Normalisation des identifiants et politique des mots de passe locaux."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from ._password_blocklist import BLOCKED_PASSWORDS

_USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}", re.ASCII)


class InvalidUsernameError(ValueError):
    """L'identifiant local ne respecte pas sa forme canonique."""


class InvalidPasswordError(ValueError):
    """Le mot de passe ne respecte pas la politique locale."""


def normalize_username(username: str) -> str:
    """Retourne la forme canonique restrictive d'un identifiant local."""

    if not isinstance(username, str):
        raise InvalidUsernameError("username must be a string")
    normalized = unicodedata.normalize("NFKC", username).strip().casefold()
    if _USERNAME_PATTERN.fullmatch(normalized) is None:
        raise InvalidUsernameError("username must use 3-64 ASCII safe characters")
    return normalized


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """Politique volontairement simple, longue et compatible passphrases."""

    minimum_length: int = 15
    maximum_length: int = 128
    blocked_passwords: frozenset[str] = BLOCKED_PASSWORDS

    def validate(self, password: str, *, username: str | None = None) -> None:
        if not isinstance(password, str):
            raise InvalidPasswordError("password must be a string")
        if len(password) < self.minimum_length:
            raise InvalidPasswordError("password is too short")
        if len(password) > self.maximum_length:
            raise InvalidPasswordError("password is too long")

        canonical_password = unicodedata.normalize("NFKC", password).casefold()
        canonical_blocklist = {
            unicodedata.normalize("NFKC", blocked).casefold()
            for blocked in self.blocked_passwords
        }
        if canonical_password in canonical_blocklist:
            raise InvalidPasswordError("password is blocked")
        if username is not None:
            normalized_username = normalize_username(username)
            if canonical_password == normalized_username:
                raise InvalidPasswordError("password cannot equal username")


class PasswordHasher(Protocol):
    """Port de hachage injectable dans le service d'authentification."""

    def hash(self, password: str) -> str: ...

    def verify(self, password: str, encoded_hash: str) -> bool: ...


class Argon2PasswordHasher:
    """Adaptateur ``pwdlib`` utilisant son profil Argon2id recommande."""

    def __init__(self) -> None:
        # Import differe : le coeur reste testable avec un fake, tandis que
        # l'application echoue explicitement au demarrage si pwdlib manque.
        from pwdlib import PasswordHash

        self._password_hash = PasswordHash.recommended()

    def hash(self, password: str) -> str:
        return self._password_hash.hash(password)

    def verify(self, password: str, encoded_hash: str) -> bool:
        return self._password_hash.verify(password, encoded_hash)
