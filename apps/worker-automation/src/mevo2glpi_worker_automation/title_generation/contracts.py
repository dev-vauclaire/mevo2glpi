"""Stable title-generation contract used by the ticket stage."""

from typing import Protocol


class TitleGeneratorProtocol(Protocol):
    """Generate a GLPI title without exposing the generation backend."""

    def generate(self, transcription: str) -> str:
        """Return a validated title for a transcription."""
        ...


__all__ = ["TitleGeneratorProtocol"]
