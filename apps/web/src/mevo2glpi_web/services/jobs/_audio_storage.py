"""Low-level canonical audio inspection shared by web use cases."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

from mevo2glpi_common.audio_files import InvalidAudioStorageKeyError


def open_audio_stream(audio_path: Path) -> tuple[BinaryIO, int]:
    """Open one regular file without following a path swapped to a symlink."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    file_descriptor = os.open(audio_path, flags)
    try:
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise InvalidAudioStorageKeyError("Audio storage path is not regular")
        return os.fdopen(file_descriptor, "rb", closefd=True), file_stat.st_size
    except BaseException:
        os.close(file_descriptor)
        raise


__all__ = ["open_audio_stream"]
