"""Provider-independent transcription contracts."""

import math
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import UUID


class TranscriptionErrorCategory(StrEnum):
    """Sanitized classification shared by client and orchestration errors."""

    UNAVAILABLE = "unavailable"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    INVALID_REQUEST = "invalid_request"
    PROTOCOL = "protocol"


@dataclass(frozen=True, slots=True)
class TranscriptionSegment:
    """Optional provider-neutral timing information for one text segment."""

    start_seconds: float
    end_seconds: float
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.start_seconds, bool) or not isinstance(
            self.start_seconds, int | float
        ):
            raise TypeError("start_seconds must be a number")
        if isinstance(self.end_seconds, bool) or not isinstance(
            self.end_seconds, int | float
        ):
            raise TypeError("end_seconds must be a number")
        start_seconds = float(self.start_seconds)
        end_seconds = float(self.end_seconds)
        if (
            not math.isfinite(start_seconds)
            or not math.isfinite(end_seconds)
            or start_seconds < 0
            or end_seconds < start_seconds
        ):
            raise ValueError("segment timestamps must define a positive interval")
        if not isinstance(self.text, str):
            raise TypeError("segment text must be a string")
        if not self.text.strip() or any(
            (ord(character) < 32 and character not in "\r\n\t") or ord(character) == 127
            for character in self.text
        ):
            raise ValueError("segment text must be non-empty and contain no controls")
        object.__setattr__(self, "start_seconds", start_seconds)
        object.__setattr__(self, "end_seconds", end_seconds)


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Provider-neutral transcription associated with its source audio."""

    audio_path: Path
    audio_uuid: UUID
    transcription: str = field(repr=False)
    segments: tuple[TranscriptionSegment, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.audio_path, Path):
            raise TypeError("audio_path must be a Path")
        if not isinstance(self.audio_uuid, UUID):
            raise TypeError("audio_uuid must be a UUID")
        if not isinstance(self.transcription, str):
            raise TypeError("transcription must be a string")
        if not isinstance(self.segments, tuple) or any(
            not isinstance(segment, TranscriptionSegment) for segment in self.segments
        ):
            raise TypeError("segments must be a tuple of TranscriptionSegment")
