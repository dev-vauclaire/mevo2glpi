"""Bounded FFmpeg decoding into the native engine's strict PCM contract."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE_HZ = 16_000
FLOAT32_BYTES = 4


class AudioNormalizationError(ValueError):
    """Report an invalid or unsupported audio without exposing FFmpeg output."""


class AudioDurationExceeded(AudioNormalizationError):
    """Report audio exceeding the configured processing duration."""


@dataclass(frozen=True, slots=True)
class NormalizedAudio:
    """Little-endian float32, mono, 16 kHz PCM ready for transcribe.cpp."""

    pcm: bytes
    duration_seconds: float


class FFmpegNormalizer:
    """Decode supported containers/codecs through a bounded subprocess."""

    def __init__(
        self,
        *,
        executable: Path,
        timeout_seconds: float,
        max_audio_seconds: float,
    ) -> None:
        self._executable = Path(executable)
        self._timeout_seconds = timeout_seconds
        self._max_audio_seconds = max_audio_seconds

    async def normalize(self, audio: bytes) -> NormalizedAudio:
        if not audio:
            raise AudioNormalizationError("audio input is empty")

        process = await asyncio.create_subprocess_exec(
            str(self._executable),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            "pipe:0",
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE_HZ),
            "-t",
            _ffmpeg_duration_limit(self._max_audio_seconds),
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            pcm, _stderr = await asyncio.wait_for(
                process.communicate(audio),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            _kill_process(process)
            await process.communicate()
            raise AudioNormalizationError("audio normalization timed out") from None
        except asyncio.CancelledError:
            _kill_process(process)
            await asyncio.shield(process.communicate())
            raise

        if process.returncode != 0 or not pcm or len(pcm) % FLOAT32_BYTES:
            raise AudioNormalizationError("audio normalization failed")

        duration_seconds = len(pcm) / (SAMPLE_RATE_HZ * FLOAT32_BYTES)
        if duration_seconds > self._max_audio_seconds:
            raise AudioDurationExceeded("audio duration exceeds configured limit")
        return NormalizedAudio(pcm=pcm, duration_seconds=duration_seconds)


def _ffmpeg_duration_limit(max_audio_seconds: float) -> str:
    """Bound decoder output while preserving enough excess to reject truncation."""
    return f"{max_audio_seconds + 1.0:.3f}"


def _kill_process(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass


__all__ = [
    "AudioDurationExceeded",
    "AudioNormalizationError",
    "FFmpegNormalizer",
    "NormalizedAudio",
]
