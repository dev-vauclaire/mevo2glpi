"""CPU-only OpenAI-compatible transcription service."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("glpi-ticketing-transcription-api")
except PackageNotFoundError:  # pragma: no cover - source tree import
    __version__ = "0.1.0"

__all__ = ["__version__"]
