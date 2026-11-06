"""Lazy adapter around the pre-1.0 transcribe.cpp Python binding."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from .contracts import (
    EngineInferenceError,
    EngineLoadError,
    TranscriptionOutput,
)


class TranscribeCppEngine:
    """Keep one CPU model and one reusable session loaded in memory."""

    def __init__(
        self,
        *,
        model_path: Path,
        cpu_threads: int,
        module_loader: Callable[[str], ModuleType] = importlib.import_module,
    ) -> None:
        self._model_path = Path(model_path)
        self._cpu_threads = cpu_threads
        self._module_loader = module_loader
        self._model: Any | None = None
        self._session: Any | None = None

    @property
    def is_ready(self) -> bool:
        return self._model is not None and self._session is not None

    def load(self) -> None:
        """Load the exact GGUF on an explicit CPU device, without fallback."""
        if self.is_ready:
            return
        if not self._model_path.is_file():
            raise EngineLoadError("configured transcription model is unavailable")

        model: Any | None = None
        try:
            module = self._module_loader("transcribe_cpp")
            cpu_device = next(
                device for device in module.backends() if device.device_type == "cpu"
            )
            model = module.Model(str(self._model_path), device=cpu_device)
            session = model.session(n_threads=self._cpu_threads)
        except Exception:
            if model is not None:
                _safe_close(model)
            raise EngineLoadError("transcription engine failed to load") from None

        self._model = model
        self._session = session

    def transcribe(
        self,
        samples: bytes,
        *,
        language: str | None,
    ) -> TranscriptionOutput:
        """Run native inference and copy only the non-sensitive result fields."""
        session = self._session
        if session is None:
            raise EngineInferenceError("transcription engine is unavailable")
        try:
            result = session.run(
                samples,
                task="transcribe",
                language=language,
                timestamps="none",
            )
            text = result.text
            detected_language = result.language or None
        except Exception:
            raise EngineInferenceError("transcription inference failed") from None
        if not isinstance(text, str):
            raise EngineInferenceError("transcription inference returned invalid data")
        if detected_language is not None and not isinstance(detected_language, str):
            detected_language = None
        return TranscriptionOutput(text=text, language=detected_language)

    def close(self) -> None:
        session, model = self._session, self._model
        self._session = None
        self._model = None
        if session is not None:
            _safe_close(session)
        if model is not None:
            _safe_close(model)


def _safe_close(resource: object) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


__all__ = ["TranscribeCppEngine"]
