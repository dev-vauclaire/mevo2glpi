"""Bounded operator smoke test for a deployed Transcribe AI simulator."""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
_BOUNDARY = "mevo2glpi-synthetic-boundary"


def main() -> int:
    """Submit and poll one synthetic job without printing its transcript."""
    api_key = os.environ.get("TRANSCRIBE_AI_SIMULATOR_API_KEY", "")
    if not api_key:
        print("TRANSCRIBE_AI_SIMULATOR_API_KEY is required", file=sys.stderr)
        return 2
    base_url = os.environ.get(
        "TRANSCRIBE_AI_SIMULATOR_SMOKE_BASE_URL",
        DEFAULT_BASE_URL,
    ).rstrip("/")
    try:
        submission = _request_json(
            f"{base_url}/api/batchTranscription/createJob",
            api_key=api_key,
            body=_multipart_audio(),
            content_type=f"multipart/form-data; boundary={_BOUNDARY}",
        )
        job_uuid = _job_uuid(submission)
        first = _get_result(base_url, api_key=api_key, job_uuid=job_uuid)
        second = _get_result(base_url, api_key=api_key, job_uuid=job_uuid)
        if _job_status(first) != "PENDING" or _job_status(second) != "COMPLETED":
            raise ValueError("unexpected polling statuses")
    except (HTTPError, URLError, OSError, KeyError, TypeError, ValueError) as error:
        print(
            f"Transcribe AI simulator smoke failed error_type={type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    print(f"Transcribe AI simulator smoke completed job_uuid={job_uuid}")
    return 0


def _get_result(base_url: str, *, api_key: str, job_uuid: str) -> object:
    query = urlencode({"job_uuid": job_uuid})
    return _request_json(
        f"{base_url}/api/batchTranscription/result?{query}",
        api_key=api_key,
    )


def _request_json(
    url: str,
    *,
    api_key: str,
    body: bytes | None = None,
    content_type: str | None = None,
) -> object:
    headers = {"X-API-KEY": api_key}
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = Request(
        url,
        data=body,
        method="POST" if body is not None else "GET",
        headers=headers,
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _multipart_audio() -> bytes:
    return (
        (
            f"--{_BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="audioFile"; filename="smoke.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        + b"RIFF-synthetic\r\n"
        + f"--{_BOUNDARY}--\r\n".encode()
    )


def _job_uuid(payload: object) -> str:
    if not isinstance(payload, dict):
        raise TypeError("submission response must be an object")
    data = payload["data"]
    if not isinstance(data, dict):
        raise TypeError("submission data must be an object")
    value = data["job_uuid"]
    if not isinstance(value, str) or not value:
        raise TypeError("job_uuid must be a non-empty string")
    return value


def _job_status(payload: object) -> str:
    if not isinstance(payload, dict):
        raise TypeError("result response must be an object")
    data = payload["data"]
    if not isinstance(data, dict):
        raise TypeError("result data must be an object")
    value = data["status"]
    if not isinstance(value, str):
        raise TypeError("job status must be a string")
    return value


__all__ = ["DEFAULT_BASE_URL", "main"]
