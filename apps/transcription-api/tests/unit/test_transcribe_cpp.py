from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest

from glpi_ticketing_transcription_api.engines import (
    EngineInferenceError,
    EngineLoadError,
    TranscribeCppEngine,
    TranscriptionOutput,
)


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, dict[str, object]]] = []
        self.closed = False

    def run(self, pcm: bytes, **kwargs: object) -> SimpleNamespace:
        self.calls.append((pcm, kwargs))
        return SimpleNamespace(text="Texte synthétique", language="fr")

    def close(self) -> None:
        self.closed = True


class FakeModel:
    def __init__(self, path: str, *, device: object) -> None:
        self.path = path
        self.device = device
        self.session_instance = FakeSession()
        self.threads: int | None = None
        self.closed = False

    def session(self, *, n_threads: int) -> FakeSession:
        self.threads = n_threads
        return self.session_instance

    def close(self) -> None:
        self.closed = True


def _engine(tmp_path: Path) -> tuple[TranscribeCppEngine, list[FakeModel]]:
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"synthetic-model")
    models: list[FakeModel] = []

    def model_factory(path: str, *, device: object) -> FakeModel:
        model = FakeModel(path, device=device)
        models.append(model)
        return model

    module = SimpleNamespace(
        backends=lambda: [
            SimpleNamespace(device_type="gpu"),
            SimpleNamespace(device_type="cpu"),
        ],
        Model=model_factory,
    )
    engine = TranscribeCppEngine(
        model_path=model_path,
        cpu_threads=3,
        module_loader=lambda _name: module,  # type: ignore[arg-type]
    )
    return engine, models


def test_engine_selects_cpu_and_reuses_loaded_session(tmp_path: Path) -> None:
    engine, models = _engine(tmp_path)

    engine.load()
    output = engine.transcribe(b"\x00\x00\x00\x00", language="fr")

    assert engine.is_ready is True
    assert len(models) == 1
    assert models[0].device.device_type == "cpu"
    assert models[0].threads == 3
    assert models[0].session_instance.calls == [
        (
            b"\x00\x00\x00\x00",
            {"task": "transcribe", "language": "fr", "timestamps": "none"},
        )
    ]
    assert output.text == "Texte synthétique"
    assert output.language == "fr"
    assert "Texte synthétique" not in repr(output)

    engine.close()
    assert engine.is_ready is False
    assert models[0].session_instance.closed is True
    assert models[0].closed is True


def test_engine_load_failure_is_sanitized(tmp_path: Path) -> None:
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"synthetic-model")
    secret = "private native loader diagnostic"
    engine = TranscribeCppEngine(
        model_path=model_path,
        cpu_threads=1,
        module_loader=lambda _name: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    with pytest.raises(EngineLoadError) as captured:
        engine.load()

    assert secret not in str(captured.value)
    assert engine.is_ready is False


def test_engine_requires_existing_model_file(tmp_path: Path) -> None:
    engine = TranscribeCppEngine(
        model_path=tmp_path / "missing.gguf",
        cpu_threads=1,
    )

    with pytest.raises(EngineLoadError):
        engine.load()


def test_engine_rejects_inference_before_load(tmp_path: Path) -> None:
    engine, _models = _engine(tmp_path)

    with pytest.raises(EngineInferenceError):
        engine.transcribe(b"\x00\x00\x00\x00", language="fr")


def test_transcription_output_never_reveals_text() -> None:
    output = TranscriptionOutput(text="secret transcript", language="fr")
    assert "secret transcript" not in repr(output)


def test_python_binding_and_native_runtime_versions_stay_aligned() -> None:
    assert version("transcribe-cpp") == "0.4.1"
    assert version("transcribe-cpp-native") == "0.4.1"
