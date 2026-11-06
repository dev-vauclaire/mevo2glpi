"""Deterministic title generator used until an LLM backend is introduced."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DefaultTitleGenerator:
    """Return one configured fallback title for every transcription."""

    title: str

    def __post_init__(self) -> None:
        _validate_title(self.title)

    def generate(self, transcription: str) -> str:
        """Return the configured title without retaining transcript content."""
        if not isinstance(transcription, str):
            raise TypeError("transcription must be a string")
        return self.title


def _validate_title(title: object) -> str:
    if not isinstance(title, str):
        raise TypeError("title must be a string")
    if (
        not title
        or title != title.strip()
        or len(title) > 255
        or any(not character.isprintable() for character in title)
    ):
        raise ValueError(
            "title must be non-empty, trimmed, printable and no longer than "
            "255 characters"
        )
    return title


__all__ = ["DefaultTitleGenerator"]
