"""Cookie de session signe, minimal et commun aux modes local et OIDC."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from .models import AuthMode, Principal, SessionPayload

_COOKIE_KEYS = frozenset(
    {"auth_mode", "subject", "roles", "session_version", "iat", "exp", "csrf"}
)
_MAX_COOKIE_LENGTH = 4096
_TOKEN_BYTES = 32


class InvalidSessionError(ValueError):
    """Le cookie est invalide, expire ou ne correspond plus au compte."""


class SessionSigner(Protocol):
    """Port de signature afin de tester le codec sans dependance concrete."""

    def dumps(self, payload: Mapping[str, object]) -> str: ...

    def loads(self, signed_value: str) -> object: ...


class ItsDangerousSessionSigner:
    """Signature HMAC-SHA256 avec ``itsdangerous``."""

    def __init__(self, secret_key: str, *, salt: str = "mevo2glpi.web.session.v1"):
        if len(secret_key.encode("utf-8")) < 32:
            raise ValueError("session signing key must contain at least 32 bytes")

        from itsdangerous import BadData, URLSafeSerializer

        self._bad_data_type = BadData
        self._serializer = URLSafeSerializer(
            secret_key=secret_key,
            salt=salt,
            serializer=json,
            signer_kwargs={"digest_method": hashlib.sha256},
        )

    def dumps(self, payload: Mapping[str, object]) -> str:
        return self._serializer.dumps(dict(payload))

    def loads(self, signed_value: str) -> object:
        try:
            return self._serializer.loads(signed_value)
        except self._bad_data_type as error:
            raise InvalidSessionError("invalid session") from error


@dataclass(frozen=True, slots=True)
class LocalSessionAccountState:
    is_active: bool
    session_version: int


class LocalSessionAccountReader(Protocol):
    """Lecture obligatoire a chaque requete pour un cookie local."""

    def get_session_state(
        self, account_id: UUID
    ) -> LocalSessionAccountState | None: ...


@dataclass(frozen=True, slots=True)
class IssuedSession:
    cookie_value: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ResolvedSession:
    """Validated session data made available to the HTTP boundary."""

    principal: Principal
    csrf_token: str
    expires_at: datetime


class SessionCookieCodec:
    """Encode et valide le schema strict du cookie signe."""

    def __init__(self, signer: SessionSigner) -> None:
        self._signer = signer

    def encode(self, payload: SessionPayload) -> str:
        principal = payload.principal
        return self._signer.dumps(
            {
                "auth_mode": principal.auth_mode.value,
                "subject": principal.subject,
                "roles": sorted(principal.roles),
                "session_version": principal.session_version,
                "iat": int(payload.issued_at.timestamp()),
                "exp": int(payload.expires_at.timestamp()),
                "csrf": payload.csrf_token,
            }
        )

    def decode(self, cookie_value: str) -> SessionPayload:
        if (
            not isinstance(cookie_value, str)
            or not cookie_value
            or len(cookie_value) > _MAX_COOKIE_LENGTH
        ):
            raise InvalidSessionError("invalid session")
        try:
            loaded = self._signer.loads(cookie_value)
            return _payload_from_mapping(loaded)
        except InvalidSessionError:
            raise
        except (TypeError, ValueError, KeyError, OverflowError) as error:
            raise InvalidSessionError("invalid session") from error


class SessionManager:
    """Emet et authentifie les sessions sans persistance serveur."""

    def __init__(
        self,
        codec: SessionCookieCodec,
        *,
        auth_mode: AuthMode,
        local_accounts: LocalSessionAccountReader | None = None,
        lifetime: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if lifetime <= timedelta(0):
            raise ValueError("session lifetime must be positive")
        if auth_mode is AuthMode.LOCAL and local_accounts is None:
            raise ValueError("local mode requires an account state reader")
        self._codec = codec
        self._auth_mode = auth_mode
        self._local_accounts = local_accounts
        self._lifetime = lifetime
        self._clock = clock or (lambda: datetime.now(UTC))
        self._random_bytes = random_bytes

    def issue(self, principal: Principal) -> IssuedSession:
        if principal.auth_mode is not self._auth_mode:
            raise InvalidSessionError("invalid session")
        now = self._utc_now()
        csrf_token = _encode_token(self._random_bytes(_TOKEN_BYTES))
        payload = SessionPayload(
            principal=principal,
            issued_at=now,
            expires_at=now + self._lifetime,
            csrf_token=csrf_token,
        )
        return IssuedSession(
            cookie_value=self._codec.encode(payload),
            csrf_token=csrf_token,
            expires_at=payload.expires_at,
        )

    def authenticate(self, cookie_value: str) -> Principal:
        return self.resolve(cookie_value).principal

    def resolve(self, cookie_value: str) -> ResolvedSession:
        """Validate a cookie and expose only the data needed by one request."""
        payload = self._validate_payload(cookie_value)
        return ResolvedSession(
            principal=payload.principal,
            csrf_token=payload.csrf_token,
            expires_at=payload.expires_at,
        )

    def validate_csrf(self, cookie_value: str, submitted_token: str) -> Principal:
        payload = self._validate_payload(cookie_value)
        if (
            not isinstance(submitted_token, str)
            or not 32 <= len(submitted_token) <= 128
            or not hmac.compare_digest(payload.csrf_token, submitted_token)
        ):
            raise InvalidSessionError("invalid CSRF token")
        return payload.principal

    def _validate_payload(self, cookie_value: str) -> SessionPayload:
        payload = self._codec.decode(cookie_value)
        now = self._utc_now()
        if (
            payload.issued_at > now
            or payload.expires_at <= now
            or payload.expires_at - payload.issued_at > self._lifetime
        ):
            raise InvalidSessionError("invalid session")

        principal = payload.principal
        if principal.auth_mode is not self._auth_mode:
            raise InvalidSessionError("invalid session")
        if principal.auth_mode is AuthMode.LOCAL:
            try:
                account_id = UUID(principal.subject)
            except ValueError as error:
                raise InvalidSessionError("invalid session") from error
            assert self._local_accounts is not None
            state = self._local_accounts.get_session_state(account_id)
            if (
                state is None
                or not state.is_active
                or state.session_version != principal.session_version
            ):
                raise InvalidSessionError("invalid session")
        return payload

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("session clock must return an aware timestamp")
        return now.astimezone(UTC)


def _payload_from_mapping(value: object) -> SessionPayload:
    if not isinstance(value, dict) or set(value) != _COOKIE_KEYS:
        raise InvalidSessionError("invalid session")

    raw_roles = value["roles"]
    if not isinstance(raw_roles, list) or not all(
        isinstance(role, str) for role in raw_roles
    ):
        raise InvalidSessionError("invalid session")
    if isinstance(value["iat"], bool) or not isinstance(value["iat"], int):
        raise InvalidSessionError("invalid session")
    if isinstance(value["exp"], bool) or not isinstance(value["exp"], int):
        raise InvalidSessionError("invalid session")

    raw_session_version = value["session_version"]
    if raw_session_version is not None and (
        isinstance(raw_session_version, bool)
        or not isinstance(raw_session_version, int)
    ):
        raise InvalidSessionError("invalid session")

    principal = Principal(
        auth_mode=AuthMode(value["auth_mode"]),
        subject=value["subject"],
        roles=frozenset(raw_roles),
        session_version=raw_session_version,
    )
    return SessionPayload(
        principal=principal,
        issued_at=datetime.fromtimestamp(value["iat"], tz=UTC),
        expires_at=datetime.fromtimestamp(value["exp"], tz=UTC),
        csrf_token=value["csrf"],
    )


def _encode_token(raw: bytes) -> str:
    if not isinstance(raw, bytes) or len(raw) != _TOKEN_BYTES:
        raise ValueError("random source must return exactly 32 bytes")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
