"""Crash-recovery and safety tests for exact-key audio purges."""

import errno
import os
from pathlib import Path
from uuid import UUID

import pytest

from glpi_ticketing_common import audio_purge
from glpi_ticketing_common.audio_purge import (
    AmbiguousAudioPurgeStateError,
    AudioPurgeDisposition,
    AudioPurgeErrorCode,
    AudioPurgeFilesystemError,
    InvalidAudioPurgeTargetError,
    UnsafeAudioPurgeStateError,
    purge_audio,
)

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
STORAGE_KEY = f"{AUDIO_UUID}.wav"
AUDIO_BYTES = b"RIFF-synthetic-audio"


def _quarantine_path(audio_root: Path) -> Path:
    return audio_root / audio_purge.PURGE_DIRECTORY_NAME / STORAGE_KEY


def test_purge_moves_then_removes_the_exact_canonical_audio(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / STORAGE_KEY
    canonical.write_bytes(AUDIO_BYTES)
    unrelated = tmp_path / f"{UUID('223e4567-e89b-42d3-a456-426614174000')}.wav"
    unrelated.write_bytes(b"must-remain")

    result = purge_audio(tmp_path, STORAGE_KEY)

    assert result.storage_key == STORAGE_KEY
    assert result.disposition is AudioPurgeDisposition.PURGED
    assert not canonical.exists()
    assert not _quarantine_path(tmp_path).exists()
    assert unrelated.read_bytes() == b"must-remain"


def test_purge_publishes_quarantine_name_without_replacing_an_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = tmp_path / STORAGE_KEY
    canonical.write_bytes(AUDIO_BYTES)
    quarantine = tmp_path / audio_purge.PURGE_DIRECTORY_NAME
    quarantine.mkdir()
    moves: list[tuple[Path, Path, bool]] = []
    real_link = os.link

    def tracked_link(
        source: Path,
        destination: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        moves.append((Path(source), Path(destination), follow_symlinks))
        real_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(audio_purge.os, "link", tracked_link)

    purge_audio(tmp_path, STORAGE_KEY)

    assert moves == [(canonical.resolve(), quarantine.resolve() / STORAGE_KEY, False)]


def test_purge_resumes_from_an_existing_quarantine_entry(
    tmp_path: Path,
) -> None:
    quarantine = tmp_path / audio_purge.PURGE_DIRECTORY_NAME
    quarantine.mkdir()
    _quarantine_path(tmp_path).write_bytes(AUDIO_BYTES)

    result = purge_audio(tmp_path, STORAGE_KEY)

    assert result.disposition is AudioPurgeDisposition.RESUMED
    assert not _quarantine_path(tmp_path).exists()


def test_purge_recovers_a_create_only_move_interrupted_with_two_hardlinks(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / STORAGE_KEY
    canonical.write_bytes(AUDIO_BYTES)
    quarantine = tmp_path / audio_purge.PURGE_DIRECTORY_NAME
    quarantine.mkdir()
    os.link(canonical, _quarantine_path(tmp_path))

    result = purge_audio(tmp_path, STORAGE_KEY)

    assert result.disposition is AudioPurgeDisposition.RESUMED
    assert not canonical.exists()
    assert not _quarantine_path(tmp_path).exists()


def test_purge_is_idempotent_when_both_entries_are_absent(tmp_path: Path) -> None:
    result = purge_audio(tmp_path, STORAGE_KEY)

    assert result.disposition is AudioPurgeDisposition.ALREADY_ABSENT
    assert (tmp_path / audio_purge.PURGE_DIRECTORY_NAME).is_dir()


def test_purge_recovers_after_a_crash_acknowledgement_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = tmp_path / STORAGE_KEY
    canonical.write_bytes(AUDIO_BYTES)
    (tmp_path / audio_purge.PURGE_DIRECTORY_NAME).mkdir()
    real_fsync = os.fsync
    calls = 0

    def fail_after_first_fsync(file_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        real_fsync(file_descriptor)
        if calls == 1:
            raise OSError(errno.EIO, "injected after durable publication")

    monkeypatch.setattr(audio_purge.os, "fsync", fail_after_first_fsync)

    with pytest.raises(AudioPurgeFilesystemError) as captured:
        purge_audio(tmp_path, STORAGE_KEY)

    assert captured.value.code is AudioPurgeErrorCode.DIRECTORY_SYNC_FAILED
    assert captured.value.retryable is True
    assert not canonical.exists()
    assert _quarantine_path(tmp_path).read_bytes() == AUDIO_BYTES

    monkeypatch.setattr(audio_purge.os, "fsync", real_fsync)
    result = purge_audio(tmp_path, STORAGE_KEY)

    assert result.disposition is AudioPurgeDisposition.RESUMED
    assert not _quarantine_path(tmp_path).exists()


def test_purge_recovers_when_unlink_succeeded_before_sync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = tmp_path / STORAGE_KEY
    canonical.write_bytes(AUDIO_BYTES)
    (tmp_path / audio_purge.PURGE_DIRECTORY_NAME).mkdir()
    real_fsync = os.fsync
    calls = 0

    def fail_after_unlink(file_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        real_fsync(file_descriptor)
        if calls == 3:
            raise OSError(errno.EIO, "injected after durable unlink")

    monkeypatch.setattr(audio_purge.os, "fsync", fail_after_unlink)

    with pytest.raises(AudioPurgeFilesystemError) as captured:
        purge_audio(tmp_path, STORAGE_KEY)

    assert captured.value.code is AudioPurgeErrorCode.DIRECTORY_SYNC_FAILED
    assert not canonical.exists()
    assert not _quarantine_path(tmp_path).exists()

    monkeypatch.setattr(audio_purge.os, "fsync", real_fsync)
    result = purge_audio(tmp_path, STORAGE_KEY)

    assert result.disposition is AudioPurgeDisposition.ALREADY_ABSENT


def test_purge_blocks_an_ambiguous_double_entry_without_deleting_either(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / STORAGE_KEY
    canonical.write_bytes(b"canonical")
    (tmp_path / audio_purge.PURGE_DIRECTORY_NAME).mkdir()
    quarantined = _quarantine_path(tmp_path)
    quarantined.write_bytes(b"quarantine")

    with pytest.raises(AmbiguousAudioPurgeStateError) as captured:
        purge_audio(tmp_path, STORAGE_KEY)

    assert captured.value.code is AudioPurgeErrorCode.AMBIGUOUS_STATE
    assert captured.value.retryable is False
    assert canonical.read_bytes() == b"canonical"
    assert quarantined.read_bytes() == b"quarantine"


def test_concurrent_quarantine_publication_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = tmp_path / STORAGE_KEY
    canonical.write_bytes(b"canonical")
    quarantine = tmp_path / audio_purge.PURGE_DIRECTORY_NAME
    quarantine.mkdir()
    quarantined = _quarantine_path(tmp_path)

    def publish_competing_entry(
        _source: Path,
        destination: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        assert follow_symlinks is False
        destination.write_bytes(b"competing")
        raise FileExistsError(errno.EEXIST, "synthetic concurrent publication")

    monkeypatch.setattr(audio_purge.os, "link", publish_competing_entry)

    with pytest.raises(AudioPurgeFilesystemError) as captured:
        purge_audio(tmp_path, STORAGE_KEY)

    assert captured.value.code is AudioPurgeErrorCode.STATE_CHANGED
    assert captured.value.retryable is True
    assert canonical.read_bytes() == b"canonical"
    assert quarantined.read_bytes() == b"competing"


@pytest.mark.parametrize("entry_location", ["canonical", "quarantine"])
def test_purge_refuses_symbolic_audio_entries(
    tmp_path: Path,
    entry_location: str,
) -> None:
    outside = tmp_path.parent / f"outside-{entry_location}.wav"
    outside.write_bytes(AUDIO_BYTES)
    if entry_location == "canonical":
        target = tmp_path / STORAGE_KEY
    else:
        (tmp_path / audio_purge.PURGE_DIRECTORY_NAME).mkdir()
        target = _quarantine_path(tmp_path)
    target.symlink_to(outside)

    with pytest.raises(UnsafeAudioPurgeStateError) as captured:
        purge_audio(tmp_path, STORAGE_KEY)

    expected_code = (
        AudioPurgeErrorCode.UNSAFE_CANONICAL_ENTRY
        if entry_location == "canonical"
        else AudioPurgeErrorCode.UNSAFE_QUARANTINE_ENTRY
    )
    assert captured.value.code is expected_code
    assert captured.value.retryable is False
    assert outside.read_bytes() == AUDIO_BYTES
    assert target.is_symlink()


@pytest.mark.parametrize("entry_location", ["canonical", "quarantine"])
def test_purge_refuses_non_regular_audio_entries(
    tmp_path: Path,
    entry_location: str,
) -> None:
    if entry_location == "canonical":
        target = tmp_path / STORAGE_KEY
    else:
        (tmp_path / audio_purge.PURGE_DIRECTORY_NAME).mkdir()
        target = _quarantine_path(tmp_path)
    target.mkdir()

    with pytest.raises(UnsafeAudioPurgeStateError):
        purge_audio(tmp_path, STORAGE_KEY)

    assert target.is_dir()


def test_purge_refuses_a_symbolic_quarantine_directory(tmp_path: Path) -> None:
    canonical = tmp_path / STORAGE_KEY
    canonical.write_bytes(AUDIO_BYTES)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / audio_purge.PURGE_DIRECTORY_NAME).symlink_to(outside)

    with pytest.raises(UnsafeAudioPurgeStateError) as captured:
        purge_audio(tmp_path, STORAGE_KEY)

    assert captured.value.code is AudioPurgeErrorCode.UNSAFE_QUARANTINE
    assert canonical.read_bytes() == AUDIO_BYTES
    assert not list(outside.iterdir())


def test_purge_refuses_a_symbolic_audio_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    canonical = real_root / STORAGE_KEY
    canonical.write_bytes(AUDIO_BYTES)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root)

    with pytest.raises(UnsafeAudioPurgeStateError) as captured:
        purge_audio(linked_root, STORAGE_KEY)

    assert captured.value.code is AudioPurgeErrorCode.UNSAFE_AUDIO_ROOT
    assert canonical.read_bytes() == AUDIO_BYTES


@pytest.mark.parametrize(
    "storage_key",
    [
        "",
        f"../{STORAGE_KEY}",
        f"directory/{STORAGE_KEY}",
        f"{str(AUDIO_UUID).upper()}.wav",
        f"{AUDIO_UUID}.WAV",
        f"{AUDIO_UUID}.wav.part",
        None,
    ],
)
def test_purge_rejects_every_non_canonical_storage_key(
    tmp_path: Path,
    storage_key: object,
) -> None:
    with pytest.raises(InvalidAudioPurgeTargetError) as captured:
        purge_audio(tmp_path, storage_key)  # type: ignore[arg-type]

    assert captured.value.code is AudioPurgeErrorCode.INVALID_STORAGE_KEY
    assert captured.value.retryable is False
    assert not (tmp_path / audio_purge.PURGE_DIRECTORY_NAME).exists()


def test_purge_filesystem_error_contains_only_sanitized_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = tmp_path / STORAGE_KEY
    canonical.write_bytes(AUDIO_BYTES)
    (tmp_path / audio_purge.PURGE_DIRECTORY_NAME).mkdir()
    sensitive_text = "secret-volume-path"

    def fail_move(
        _source: Path,
        _destination: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        assert follow_symlinks is False
        raise OSError(errno.EIO, sensitive_text, str(canonical))

    monkeypatch.setattr(audio_purge.os, "link", fail_move)

    with pytest.raises(AudioPurgeFilesystemError) as captured:
        purge_audio(tmp_path, STORAGE_KEY)

    error = captured.value
    assert error.code is AudioPurgeErrorCode.MOVE_FAILED
    assert error.retryable is True
    assert error.errno == errno.EIO
    assert str(error) == AudioPurgeErrorCode.MOVE_FAILED.value
    assert sensitive_text not in repr(error)
    assert str(tmp_path) not in repr(error)
    assert error.__cause__ is None
    assert canonical.read_bytes() == AUDIO_BYTES
