"""Polling scanner for the automation worker."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Never

from glpi_ticketing_common import audio_files


def run_once(
    *,
    pending_directory: Path,
    processing_directory: Path,
) -> Path | None:
    """Return existing processing work or claim the next pending audio."""
    processing_files = audio_files.list_ready_audio_files(processing_directory)
    if processing_files:
        return processing_files[0]

    pending_files = audio_files.list_ready_audio_files(pending_directory)
    if not pending_files:
        return None

    return audio_files.move_audio(pending_files[0], processing_directory)


def run_forever(
    *,
    pending_directory: Path,
    processing_directory: Path,
    poll_interval_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Never:
    """Poll indefinitely and wait only while no audio is available."""
    while True:
        claimed_audio = run_once(
            pending_directory=pending_directory,
            processing_directory=processing_directory,
        )
        if claimed_audio is None:
            sleep(poll_interval_seconds)
