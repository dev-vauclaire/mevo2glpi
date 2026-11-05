"""Thread-safe ephemeral state for the Microsoft Graph staging contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from uuid import UUID, uuid4

from .audio import SyntheticAudioExtension
from .scenarios import (
    SeedScenario,
    SyntheticAttachmentKind,
    SyntheticAttachmentTemplate,
    build_scenario_attachments,
    build_uploaded_audio_attachment,
)

INBOX_FOLDER_ID = "inbox"
SYNTHETIC_VOICE_MESSAGE_SUBJECT = (
    "Message vocale de la part de06 78 37 43 63pourMEVO CH-VAUCLAIRE 29 34 23 12 32"
)


@dataclass(frozen=True, slots=True)
class SyntheticAttachment:
    """One fake attachment held only in process memory."""

    attachment_id: str
    name: str
    media_type: str
    content: bytes = field(repr=False)
    size_bytes: int
    kind: SyntheticAttachmentKind = SyntheticAttachmentKind.FILE
    is_inline: bool = False


@dataclass(slots=True)
class SyntheticMessage:
    """One Exchange-like message with a stable immutable identifier."""

    message_id: str
    parent_folder_id: str
    received_at: datetime
    subject: str = field(repr=False)
    attachments: tuple[SyntheticAttachment, ...]
    scenario: SeedScenario | None


class MicrosoftGraphSimulatorState:
    """Own mutable Graph state without persistence across restarts."""

    def __init__(self, *, mailbox_id: UUID) -> None:
        if not isinstance(mailbox_id, UUID):
            raise TypeError("mailbox_id must be a UUID")
        self.mailbox_id = mailbox_id
        self._lock = RLock()
        self._folders: dict[str, str] = {INBOX_FOLDER_ID: "Inbox"}
        self._messages: dict[str, SyntheticMessage] = {}
        self._client_request_ids: set[UUID] = set()

    def reset(self) -> None:
        """Drop all synthetic messages and acknowledgement folders."""
        with self._lock:
            self._folders = {INBOX_FOLDER_ID: "Inbox"}
            self._messages.clear()
            self._client_request_ids.clear()

    def register_client_request_id(self, request_id: UUID) -> bool:
        """Return false when a Graph client-request-id is reused."""
        with self._lock:
            if request_id in self._client_request_ids:
                return False
            self._client_request_ids.add(request_id)
            return True

    def seed_scenario(
        self,
        *,
        scenario: SeedScenario,
        received_at: datetime,
    ) -> SyntheticMessage:
        """Create one Inbox message from a named deterministic scenario."""
        if not isinstance(scenario, SeedScenario):
            raise TypeError("scenario must be a SeedScenario")
        return self._seed_message(
            attachments=build_scenario_attachments(scenario),
            received_at=received_at,
            scenario=scenario,
        )

    def seed_uploaded_audio(
        self,
        *,
        extension: SyntheticAudioExtension,
        content: bytes,
        received_at: datetime,
    ) -> SyntheticMessage:
        """Create one Inbox message from operator-provided test audio."""
        return self._seed_message(
            attachments=(build_uploaded_audio_attachment(extension, content),),
            received_at=received_at,
            scenario=None,
        )

    def _seed_message(
        self,
        *,
        attachments: tuple[SyntheticAttachmentTemplate, ...],
        received_at: datetime,
        scenario: SeedScenario | None,
    ) -> SyntheticMessage:
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        identifier = uuid4()
        message = SyntheticMessage(
            message_id=f"message-{identifier}",
            parent_folder_id=INBOX_FOLDER_ID,
            received_at=received_at.astimezone(UTC),
            subject=SYNTHETIC_VOICE_MESSAGE_SUBJECT,
            attachments=tuple(
                self._attachment_from_template(identifier, index, template)
                for index, template in enumerate(attachments, start=1)
            ),
            scenario=scenario,
        )
        with self._lock:
            self._messages[message.message_id] = message
        return message

    @staticmethod
    def _attachment_from_template(
        message_identifier: UUID,
        index: int,
        template: SyntheticAttachmentTemplate,
    ) -> SyntheticAttachment:
        return SyntheticAttachment(
            attachment_id=f"attachment-{message_identifier}-{index}",
            name=template.name,
            media_type=template.media_type,
            content=template.content,
            size_bytes=template.size_bytes,
            kind=template.kind,
            is_inline=template.is_inline,
        )

    def list_inbox_messages(self) -> tuple[SyntheticMessage, ...]:
        """Return Inbox messages oldest first, like the pinned Graph query."""
        with self._lock:
            messages = tuple(
                message
                for message in self._messages.values()
                if message.parent_folder_id == INBOX_FOLDER_ID
            )
        return tuple(
            sorted(messages, key=lambda item: (item.received_at, item.message_id))
        )

    def get_message(self, message_id: str) -> SyntheticMessage | None:
        """Return one immutable message identifier in its current folder."""
        with self._lock:
            return self._messages.get(message_id)

    def list_folders(self) -> tuple[tuple[str, str], ...]:
        """Return stable folder identifiers ordered deterministically."""
        with self._lock:
            return tuple(sorted(self._folders.items()))

    def ensure_folder(self, display_name: str) -> tuple[str, str]:
        """Find or create a top-level acknowledgement folder."""
        normalized_name = display_name.strip()
        if not normalized_name:
            raise ValueError("display_name must not be empty")
        with self._lock:
            for folder_id, current_name in self._folders.items():
                if current_name.casefold() == normalized_name.casefold():
                    return folder_id, current_name
            folder_id = f"folder-{uuid4()}"
            self._folders[folder_id] = normalized_name
            return folder_id, normalized_name

    def move_message(
        self,
        message_id: str,
        destination_folder_id: str,
    ) -> SyntheticMessage | None:
        """Move a message while preserving its immutable identifier."""
        with self._lock:
            message = self._messages.get(message_id)
            if message is None or destination_folder_id not in self._folders:
                return None
            message.parent_folder_id = destination_folder_id
            return message


__all__ = [
    "INBOX_FOLDER_ID",
    "MicrosoftGraphSimulatorState",
    "SYNTHETIC_VOICE_MESSAGE_SUBJECT",
    "SyntheticAttachment",
    "SyntheticMessage",
]
