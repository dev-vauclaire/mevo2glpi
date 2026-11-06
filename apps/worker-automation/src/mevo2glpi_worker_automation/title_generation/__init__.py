"""Provider-independent GLPI title generation."""

from .contracts import TitleGeneratorProtocol
from .default import DefaultTitleGenerator

__all__ = ["DefaultTitleGenerator", "TitleGeneratorProtocol"]
