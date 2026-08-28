"""HTTP adapter for the pinned GLPI API V2 contract."""

from __future__ import annotations

import html
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

import httpx
from pydantic import SecretStr

from .contracts import (
    GlpiClientError,
    GlpiCreateOutcomeUnknownError,
    GlpiDuplicateExternalIdError,
    GlpiHttpError,
    GlpiProtocolError,
    GlpiTicketNotFoundError,
    GlpiTicketRejectedError,
    GlpiTransportError,
)
from .models import (
    GlpiErrorCategory,
    GlpiOperation,
    GlpiTicketCreateCommand,
    GlpiTicketSnapshot,
    OAuthToken,
    TicketId,
    TicketStatus,
)

GLPI_API_VERSION = "2.3.0"
GLPI_API_ROOT = f"/api.php/v{GLPI_API_VERSION}"
TOKEN_PATH = f"{GLPI_API_ROOT}/token"
TICKET_PATH = f"{GLPI_API_ROOT}/Assistance/Ticket"
OAUTH_SCOPE = "api"
TOKEN_RENEWAL_MAX_SKEW_SECONDS = 30.0
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})
GLPI_INVALID_PARAMETER_STATUS = "ERROR_INVALID_PARAMETER"
TICKET_REJECTION_VIOLATIONS = {
    "content": frozenset({"maxLength", "pattern"}),
    "external_id": frozenset({"maxLength", "pattern"}),
}

_ResultT = TypeVar("_ResultT")


def _no_op() -> None:
    return None


@dataclass(frozen=True, slots=True)
class _CachedOAuthToken:
    token: OAuthToken
    expires_at: float


class GlpiClient:
    """Authenticate and retrieve or create tickets through GLPI API V2.3.0."""

    def __init__(
        self,
        *,
        base_url: str | httpx.URL,
        username: str,
        password: SecretStr,
        client_id: str,
        client_secret: SecretStr,
        http_client: httpx.Client,
        timeout: float | httpx.Timeout,
        monotonic: Callable[[], float] = time.monotonic,
        ownership_guard: Callable[[], None] = _no_op,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        self._username = _validate_identifier(username, "username")
        self._password = _validate_secret(password, "password")
        self._client_id = _validate_identifier(client_id, "client_id")
        self._client_secret = _validate_secret(client_secret, "client_secret")
        if not isinstance(http_client, httpx.Client):
            raise TypeError("http_client must be an httpx.Client")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        if not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")

        self._http_client = http_client
        self._timeout = _validate_timeout(timeout)
        self._monotonic = monotonic
        self._ownership_guard = ownership_guard
        self._cached_token: _CachedOAuthToken | None = None

    def authenticate(self) -> OAuthToken:
        """Obtain or reuse a validated OAuth Password access token."""
        self._ownership_guard()
        now = self._read_monotonic()
        if self._cached_token is not None and _token_is_reusable(
            self._cached_token,
            now,
        ):
            return self._cached_token.token

        operation = GlpiOperation.AUTHENTICATE
        try:
            response = self._guarded_request(
                lambda: self._http_client.post(
                    self._url(TOKEN_PATH),
                    headers={"Accept": "application/json"},
                    data={
                        "grant_type": "password",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret.get_secret_value(),
                        "username": self._username,
                        "password": self._password.get_secret_value(),
                        "scope": OAUTH_SCOPE,
                    },
                    timeout=self._timeout,
                    follow_redirects=False,
                )
            )
        except httpx.RequestError:
            raise GlpiTransportError(operation) from None

        if response.status_code != httpx.codes.OK:
            raise _http_error(operation, response.status_code)

        payload = _decode_object(response, operation)
        token_type = _require_non_empty_string(
            payload,
            "token_type",
            operation,
            "invalid_token_type",
        )
        if token_type != "Bearer":
            raise GlpiProtocolError(operation, "unsupported_token_type")
        access_token = _require_non_empty_string(
            payload,
            "access_token",
            operation,
            "invalid_access_token",
        )
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, bool) or not isinstance(expires_in, int):
            raise GlpiProtocolError(operation, "invalid_token_expiry")
        if expires_in <= 0:
            raise GlpiProtocolError(operation, "invalid_token_expiry")

        token = OAuthToken(
            token_type=token_type,
            access_token=SecretStr(access_token),
            expires_in_seconds=expires_in,
        )
        self._cached_token = _CachedOAuthToken(
            token=token,
            expires_at=self._read_monotonic() + expires_in,
        )
        return token

    def ensure_ticket(self, command: GlpiTicketCreateCommand) -> TicketId:
        """Return a ticket correlated to the audio UUID, creating it if absent."""
        if not isinstance(command, GlpiTicketCreateCommand):
            raise TypeError("command must be a GlpiTicketCreateCommand")
        return self._ensure_ticket(command, authentication_retry_available=True)

    def get_ticket(self, ticket_id: TicketId) -> GlpiTicketSnapshot:
        """Load one ticket directly from its persisted GLPI identifier."""
        if (
            isinstance(ticket_id, bool)
            or not isinstance(ticket_id, int)
            or ticket_id <= 0
        ):
            raise ValueError("ticket_id must be a positive integer")
        return self._get_ticket(
            TicketId(ticket_id),
            authentication_retry_available=True,
        )

    def _ensure_ticket(
        self,
        command: GlpiTicketCreateCommand,
        *,
        authentication_retry_available: bool,
    ) -> TicketId:
        token = self.authenticate()
        external_id = str(command.audio_uuid)

        try:
            existing_ticket_id = self._find_ticket(external_id, token)
        except GlpiHttpError as error:
            if _is_unauthorized(error) and authentication_retry_available:
                self._invalidate_token()
                return self._ensure_ticket(
                    command,
                    authentication_retry_available=False,
                )
            raise

        if existing_ticket_id is not None:
            return existing_ticket_id

        try:
            return self._create_ticket(command, external_id, token)
        except GlpiHttpError as error:
            if _is_unauthorized(error) and authentication_retry_available:
                self._invalidate_token()
                return self._ensure_ticket(
                    command,
                    authentication_retry_available=False,
                )
            raise
        except GlpiCreateOutcomeUnknownError as error:
            return self._reconcile_unknown_creation(external_id, error)

    def _get_ticket(
        self,
        ticket_id: TicketId,
        *,
        authentication_retry_available: bool,
    ) -> GlpiTicketSnapshot:
        operation = GlpiOperation.GET_TICKET
        token = self.authenticate()
        try:
            response = self._guarded_request(
                lambda: self._http_client.get(
                    self._url(f"{TICKET_PATH}/{int(ticket_id)}"),
                    headers=self._bearer_headers(token),
                    timeout=self._timeout,
                    follow_redirects=False,
                )
            )
        except httpx.RequestError:
            raise GlpiTransportError(operation) from None

        if response.status_code == httpx.codes.UNAUTHORIZED:
            if authentication_retry_available:
                self._invalidate_token()
                return self._get_ticket(
                    ticket_id,
                    authentication_retry_available=False,
                )
            raise _http_error(operation, response.status_code)
        if response.status_code == httpx.codes.NOT_FOUND:
            raise GlpiTicketNotFoundError
        if response.status_code != httpx.codes.OK:
            if 200 <= response.status_code < 400:
                raise GlpiProtocolError(operation, "unexpected_get_status")
            raise _http_error(operation, response.status_code)

        payload = _decode_object(response, operation)
        returned_ticket_id = _require_positive_id(payload, operation)
        if returned_ticket_id != int(ticket_id):
            raise GlpiProtocolError(operation, "mismatched_ticket_id")
        external_id = _require_non_empty_string(
            payload,
            "external_id",
            operation,
            "invalid_ticket_external_id",
        )
        status = _require_ticket_status(payload, operation)
        return GlpiTicketSnapshot(
            ticket_id=TicketId(returned_ticket_id),
            external_id=external_id,
            status=status,
        )

    def _find_ticket(
        self,
        external_id: str,
        token: OAuthToken,
    ) -> TicketId | None:
        operation = GlpiOperation.FIND_TICKET
        try:
            response = self._guarded_request(
                lambda: self._http_client.get(
                    self._url(TICKET_PATH),
                    headers=self._bearer_headers(token),
                    params={
                        "filter": f"external_id=={external_id}",
                        "limit": "2",
                    },
                    timeout=self._timeout,
                    follow_redirects=False,
                )
            )
        except httpx.RequestError:
            raise GlpiTransportError(operation) from None

        if response.status_code not in {httpx.codes.OK, httpx.codes.PARTIAL_CONTENT}:
            raise _http_error(operation, response.status_code)

        payload = _decode_array(response, operation)
        if response.status_code == httpx.codes.PARTIAL_CONTENT or len(payload) > 1:
            raise GlpiDuplicateExternalIdError
        if not payload:
            return None

        ticket_payload = payload[0]
        if not isinstance(ticket_payload, Mapping):
            raise GlpiProtocolError(operation, "invalid_ticket_search_item")
        if ticket_payload.get("external_id") != external_id:
            raise GlpiProtocolError(operation, "mismatched_external_id")
        return TicketId(_require_positive_id(ticket_payload, operation))

    def _create_ticket(
        self,
        command: GlpiTicketCreateCommand,
        external_id: str,
        token: OAuthToken,
    ) -> TicketId:
        operation = GlpiOperation.CREATE_TICKET
        try:
            response = self._guarded_request(
                lambda: self._http_client.post(
                    self._url(TICKET_PATH),
                    headers=self._bearer_headers(token),
                    json={
                        "name": command.defaults.title,
                        "content": _build_ticket_content(command),
                        "external_id": external_id,
                        "type": int(command.defaults.ticket_type),
                        "urgency": int(command.defaults.urgency),
                        "impact": int(command.defaults.impact),
                    },
                    timeout=self._timeout,
                    follow_redirects=False,
                )
            )
        except httpx.RequestError:
            raise GlpiCreateOutcomeUnknownError(
                category=GlpiErrorCategory.UNAVAILABLE,
                technical_code="transport_error",
            ) from None

        if response.status_code != httpx.codes.CREATED:
            if _is_retryable_http_status(response.status_code):
                raise GlpiCreateOutcomeUnknownError(
                    category=GlpiErrorCategory.UNAVAILABLE,
                    technical_code=f"http_{response.status_code}",
                    status_code=response.status_code,
                )
            if 200 <= response.status_code < 400:
                raise GlpiCreateOutcomeUnknownError(
                    category=GlpiErrorCategory.PROTOCOL,
                    technical_code="unexpected_create_status",
                    status_code=response.status_code,
                )
            if _is_allowlisted_ticket_rejection(response):
                raise GlpiTicketRejectedError
            raise _http_error(operation, response.status_code)

        try:
            payload = _decode_object(response, operation)
            ticket_id = _require_positive_id(payload, operation)
        except GlpiProtocolError as error:
            raise GlpiCreateOutcomeUnknownError(
                category=GlpiErrorCategory.PROTOCOL,
                technical_code=error.technical_code,
                status_code=response.status_code,
            ) from None
        return TicketId(ticket_id)

    def _reconcile_unknown_creation(
        self,
        external_id: str,
        outcome_error: GlpiCreateOutcomeUnknownError,
    ) -> TicketId:
        authentication_retry_available = True
        while True:
            try:
                token = self.authenticate()
                ticket_id = self._find_ticket(external_id, token)
            except GlpiDuplicateExternalIdError:
                raise
            except GlpiHttpError as error:
                if _is_unauthorized(error) and authentication_retry_available:
                    authentication_retry_available = False
                    self._invalidate_token()
                    continue
                raise outcome_error from None
            except GlpiClientError:
                raise outcome_error from None

            if ticket_id is not None:
                return ticket_id
            raise outcome_error

    def _bearer_headers(self, token: OAuthToken) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": (
                f"{token.token_type} {token.access_token.get_secret_value()}"
            ),
        }

    def _invalidate_token(self) -> None:
        self._cached_token = None

    def _url(self, path: str) -> httpx.URL:
        return httpx.URL(f"{str(self._base_url).rstrip('/')}{path}")

    def _read_monotonic(self) -> float:
        value = self._monotonic()
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("monotonic must return a number")
        if not math.isfinite(value):
            raise ValueError("monotonic must return a finite number")
        return float(value)

    def _guarded_request(self, operation: Callable[[], _ResultT]) -> _ResultT:
        """Fence each GLPI HTTP request with singleton ownership checks."""

        self._ownership_guard()
        try:
            return operation()
        finally:
            self._ownership_guard()


def _token_is_reusable(cached_token: _CachedOAuthToken, now: float) -> bool:
    renewal_skew = min(
        TOKEN_RENEWAL_MAX_SKEW_SECONDS,
        cached_token.token.expires_in_seconds * 0.1,
    )
    return now < cached_token.expires_at - renewal_skew


def _build_ticket_content(command: GlpiTicketCreateCommand) -> str:
    transcription = html.escape(command.transcription, quote=True)
    transcription = "<br>\n".join(transcription.splitlines())
    audio_url = html.escape(command.audio_url, quote=True)
    return f'<p>{transcription}</p><p>Audio : <a href="{audio_url}">{audio_url}</a></p>'


def _decode_object(
    response: httpx.Response,
    operation: GlpiOperation,
) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        raise GlpiProtocolError(operation, "invalid_json") from None
    if not isinstance(payload, Mapping):
        raise GlpiProtocolError(operation, "expected_json_object")
    return payload


def _decode_array(
    response: httpx.Response,
    operation: GlpiOperation,
) -> list[object]:
    try:
        payload = response.json()
    except ValueError:
        raise GlpiProtocolError(operation, "invalid_json") from None
    if not isinstance(payload, list):
        raise GlpiProtocolError(operation, "expected_json_array")
    return payload


def _require_non_empty_string(
    payload: Mapping[str, object],
    field_name: str,
    operation: GlpiOperation,
    technical_code: str,
) -> str:
    value = payload.get(field_name)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(not character.isprintable() for character in value)
    ):
        raise GlpiProtocolError(operation, technical_code)
    return value


def _require_positive_id(
    payload: Mapping[str, object],
    operation: GlpiOperation,
) -> int:
    ticket_id = payload.get("id")
    if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id <= 0:
        raise GlpiProtocolError(operation, "invalid_ticket_id")
    return ticket_id


def _require_ticket_status(
    payload: Mapping[str, object],
    operation: GlpiOperation,
) -> TicketStatus:
    status_payload = payload.get("status")
    if not isinstance(status_payload, Mapping):
        raise GlpiProtocolError(operation, "invalid_ticket_status")

    status_id = status_payload.get("id")
    if isinstance(status_id, bool) or not isinstance(status_id, int):
        raise GlpiProtocolError(operation, "invalid_ticket_status")
    try:
        return TicketStatus(status_id)
    except ValueError:
        raise GlpiProtocolError(operation, "unsupported_ticket_status") from None


def _http_error(operation: GlpiOperation, status_code: int) -> GlpiHttpError:
    retryable = _is_retryable_http_status(status_code)
    if retryable:
        category = GlpiErrorCategory.UNAVAILABLE
    elif status_code == httpx.codes.UNAUTHORIZED or (
        operation is GlpiOperation.AUTHENTICATE
        and status_code == httpx.codes.BAD_REQUEST
    ):
        category = GlpiErrorCategory.AUTHENTICATION
    elif status_code == httpx.codes.FORBIDDEN:
        category = GlpiErrorCategory.AUTHORIZATION
    else:
        category = GlpiErrorCategory.INVALID_REQUEST
    return GlpiHttpError(
        operation=operation,
        category=category,
        status_code=status_code,
        retryable=retryable,
    )


def _is_allowlisted_ticket_rejection(response: httpx.Response) -> bool:
    """Recognize only per-audio validation failures from GLPI V2.3.0.

    ``ERROR_INVALID_PARAMETER`` alone is insufficient: it can also identify a
    systemic configuration or contract problem. A terminal rejection therefore
    requires an HTTP 400 response whose validation details exclusively target
    dynamic ticket fields and contain only validation reasons observed in the
    pinned GLPI contract. Titles, messages and other response fields are ignored.
    """
    if response.status_code != httpx.codes.BAD_REQUEST:
        return False

    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, Mapping):
        return False
    if payload.get("status") != GLPI_INVALID_PARAMETER_STATUS:
        return False

    details = payload.get("detail")
    if not isinstance(details, Mapping) or not details:
        return False

    for field_name, violations in details.items():
        allowed_violations = TICKET_REJECTION_VIOLATIONS.get(field_name)
        if (
            allowed_violations is None
            or not isinstance(violations, list)
            or not violations
        ):
            return False
        for violation in violations:
            if (
                not isinstance(violation, Mapping)
                or violation.get("error") not in allowed_violations
            ):
                return False
    return True


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code in RETRYABLE_HTTP_STATUSES or 500 <= status_code < 600


def _is_unauthorized(error: GlpiHttpError) -> bool:
    return error.status_code == httpx.codes.UNAUTHORIZED


def _validate_base_url(base_url: str | httpx.URL) -> httpx.URL:
    try:
        url = httpx.URL(base_url)
    except (TypeError, ValueError) as error:
        raise ValueError("base_url must be a valid HTTP URL") from error
    if url.scheme not in {"http", "https"} or url.host is None:
        raise ValueError("base_url must be an absolute HTTP URL")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError(
            "base_url must not contain credentials, a query, or a fragment"
        )
    return url


def _validate_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if (
        not value
        or value != value.strip()
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(f"{field_name} must be non-empty, trimmed and printable")
    return value


def _validate_secret(secret: object, field_name: str) -> SecretStr:
    if not isinstance(secret, SecretStr):
        raise TypeError(f"{field_name} must be a SecretStr")
    value = secret.get_secret_value()
    if (
        not value
        or value != value.strip()
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(f"{field_name} must be non-empty, trimmed and printable")
    return secret


def _validate_timeout(timeout: float | httpx.Timeout) -> float | httpx.Timeout:
    if isinstance(timeout, bool):
        raise TypeError("timeout must be a positive number or httpx.Timeout")
    if isinstance(timeout, int | float):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and greater than zero")
        return float(timeout)
    if isinstance(timeout, httpx.Timeout):
        phases = (timeout.connect, timeout.read, timeout.write, timeout.pool)
        if any(
            phase is None or not math.isfinite(phase) or phase <= 0 for phase in phases
        ):
            raise ValueError("all timeout phases must be finite and greater than zero")
        return timeout
    raise TypeError("timeout must be a positive number or httpx.Timeout")
