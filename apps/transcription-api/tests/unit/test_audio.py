from __future__ import annotations

import asyncio

import pytest

from glpi_ticketing_transcription_api.audio import (
    AudioDurationExceeded,
    AudioNormalizationError,
    FFmpegNormalizer,
)


class FakeProcess:
    def __init__(self, *, stdout: bytes, returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode
        self.input: bytes | None = None
        self.killed = False

    async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
        self.input = data
        return self._stdout, b"private ffmpeg diagnostic"

    def kill(self) -> None:
        self.killed = True


class HangingProcess(FakeProcess):
    async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
        if data is not None:
            await asyncio.Future()
        return b"", b""


@pytest.mark.asyncio
async def test_normalizer_requests_float32_mono_16khz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(stdout=b"\x00" * (16_000 * 4))
    captured: list[object] = []

    async def create_process(*args: object, **kwargs: object) -> FakeProcess:
        captured.extend(args)
        captured.append(kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    normalizer = FFmpegNormalizer(
        executable="/usr/bin/ffmpeg",  # type: ignore[arg-type]
        timeout_seconds=5,
        max_audio_seconds=10,
    )

    result = await normalizer.normalize(b"synthetic-container")

    assert result.duration_seconds == 1
    assert process.input == b"synthetic-container"
    command = [item for item in captured if isinstance(item, str)]
    assert command[0] == "/usr/bin/ffmpeg"
    assert "pcm_f32le" in command
    assert "16000" in command
    assert "f32le" in command
    assert "pipe:0" in command
    assert "pipe:1" in command


@pytest.mark.asyncio
async def test_normalizer_rejects_failed_or_empty_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(stdout=b"", returncode=1)

    async def create_process(*_args: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    normalizer = FFmpegNormalizer(
        executable="/usr/bin/ffmpeg",  # type: ignore[arg-type]
        timeout_seconds=5,
        max_audio_seconds=10,
    )

    with pytest.raises(AudioNormalizationError) as captured:
        await normalizer.normalize(b"invalid")

    assert "private ffmpeg diagnostic" not in str(captured.value)


@pytest.mark.asyncio
async def test_normalizer_rejects_audio_over_duration_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(stdout=b"\x00" * (16_000 * 4 * 2))

    async def create_process(*_args: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    normalizer = FFmpegNormalizer(
        executable="/usr/bin/ffmpeg",  # type: ignore[arg-type]
        timeout_seconds=5,
        max_audio_seconds=1,
    )

    with pytest.raises(AudioDurationExceeded):
        await normalizer.normalize(b"synthetic-container")


@pytest.mark.asyncio
async def test_normalizer_rejects_empty_input_before_starting_ffmpeg() -> None:
    normalizer = FFmpegNormalizer(
        executable="/usr/bin/ffmpeg",  # type: ignore[arg-type]
        timeout_seconds=5,
        max_audio_seconds=10,
    )

    with pytest.raises(AudioNormalizationError, match="empty"):
        await normalizer.normalize(b"")


@pytest.mark.asyncio
async def test_normalizer_kills_timed_out_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HangingProcess(stdout=b"")

    async def create_process(*_args: object, **_kwargs: object) -> HangingProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    normalizer = FFmpegNormalizer(
        executable="/usr/bin/ffmpeg",  # type: ignore[arg-type]
        timeout_seconds=0.001,
        max_audio_seconds=10,
    )

    with pytest.raises(AudioNormalizationError, match="timed out"):
        await normalizer.normalize(b"synthetic-container")

    assert process.killed is True
