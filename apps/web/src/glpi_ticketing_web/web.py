"""FastAPI application for uploading and delivering audio files."""

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, BinaryIO, cast
from uuid import UUID, uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from glpi_ticketing_common.audio_files import (
    SUPPORTED_AUDIO_EXTENSIONS,
    atomic_write,
)
from glpi_ticketing_web import __version__
from glpi_ticketing_web.settings import DEFAULT_MAX_UPLOAD_BYTES

_AUDIO_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}
_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


@dataclass(frozen=True, slots=True)
class _WebConfig:
    pending_directory: Path
    completed_directory: Path
    max_upload_bytes: int


class _UploadTooLargeError(Exception):
    """Raised when an upload stream exceeds its configured size limit."""


class _SizeLimitedReader:
    """Read a binary stream without accepting more than a fixed byte count."""

    def __init__(self, source: BinaryIO, max_bytes: int) -> None:
        self._source = source
        self._max_bytes = max_bytes
        self._bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        """Return the next chunk or raise when one byte exceeds the limit."""
        if size == 0:
            return b""

        remaining_bytes = self._max_bytes - self._bytes_read
        probe_size = remaining_bytes + 1
        requested_size = probe_size if size < 0 else min(size, probe_size)
        chunk = self._source.read(requested_size)

        if not isinstance(chunk, bytes):
            raise TypeError("Upload stream must return bytes")

        self._bytes_read += len(chunk)
        if self._bytes_read > self._max_bytes:
            raise _UploadTooLargeError
        return chunk


def create_app(
    *,
    pending_directory: Path,
    completed_directory: Path,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> FastAPI:
    """Create the web application with only its authorized storage paths."""
    config = _build_web_config(
        pending_directory=pending_directory,
        completed_directory=completed_directory,
        max_upload_bytes=max_upload_bytes,
    )
    app = FastAPI(
        title="GLPI ticketing automation",
        version=__version__,
    )
    app.state.web_config = config

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={"max_upload_bytes": config.max_upload_bytes},
        )

    @app.post(
        "/audios",
        response_class=HTMLResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def upload_audio(
        request: Request,
        audio_file: Annotated[UploadFile, File(description="Audio file to process")],
    ) -> Response:
        try:
            extension = _validate_upload_extension(audio_file.filename)
            audio_uuid = uuid4()
            destination = config.pending_directory / f"{audio_uuid}{extension}"
            limited_reader = _SizeLimitedReader(
                cast(BinaryIO, audio_file.file),
                config.max_upload_bytes,
            )

            try:
                atomic_write(destination, cast(BinaryIO, limited_reader))
            except _UploadTooLargeError as error:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="Audio file exceeds the upload size limit",
                ) from error
        finally:
            audio_file.file.close()

        audio_uuid_text = str(audio_uuid)
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="uploaded.html",
            context={"audio_uuid": audio_uuid_text},
            status_code=status.HTTP_202_ACCEPTED,
            headers={"Location": f"/audios/{audio_uuid_text}"},
        )

    @app.get("/audios/{audio_uuid}")
    def download_audio(audio_uuid: UUID) -> FileResponse:
        audio_path = _find_completed_audio(config.completed_directory, audio_uuid)
        if audio_path is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Audio file not found",
            )

        return FileResponse(
            audio_path,
            media_type=_AUDIO_MEDIA_TYPES[audio_path.suffix],
            headers={"X-Audio-UUID": str(audio_uuid)},
        )

    return app


def _build_web_config(
    *,
    pending_directory: Path,
    completed_directory: Path,
    max_upload_bytes: int,
) -> _WebConfig:
    if isinstance(max_upload_bytes, bool) or not isinstance(max_upload_bytes, int):
        raise TypeError("max_upload_bytes must be an integer")
    if max_upload_bytes <= 0:
        raise ValueError("max_upload_bytes must be greater than zero")

    pending_directory = _resolve_directory(pending_directory)
    completed_directory = _resolve_directory(completed_directory)
    if pending_directory == completed_directory:
        raise ValueError("Pending and completed directories must be distinct")

    return _WebConfig(
        pending_directory=pending_directory,
        completed_directory=completed_directory,
        max_upload_bytes=max_upload_bytes,
    )


def _resolve_directory(directory: Path) -> Path:
    resolved_directory = Path(directory).expanduser().resolve(strict=True)
    if not resolved_directory.is_dir():
        raise NotADirectoryError(resolved_directory)
    return resolved_directory


def _validate_upload_extension(filename: str | None) -> str:
    extension = Path(filename).suffix.lower() if filename else ""
    if extension not in SUPPORTED_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported audio file extension",
        )
    return extension


def _find_completed_audio(directory: Path, audio_uuid: UUID) -> Path | None:
    matches: list[Path] = []
    for extension in _AUDIO_MEDIA_TYPES:
        candidate = directory / f"{audio_uuid}{extension}"
        try:
            file_mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISREG(file_mode):
            matches.append(candidate)

    if len(matches) > 1:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Audio storage invariant violated",
        )
    return matches[0] if matches else None
