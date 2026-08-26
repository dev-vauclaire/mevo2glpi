"""Opt-in contract test against a real transcription service."""

import os
import time
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from glpi_ticketing_worker_automation.transcription import (
    JobStatus,
    TranscriptionClient,
)

pytestmark = [pytest.mark.live_transcribe, pytest.mark.timeout(330)]

POLL_INTERVAL_SECONDS = 2.0
JOB_TIMEOUT_SECONDS = 5 * 60.0


def test_transcription_client_live_flow() -> None:
    """Submit, poll once per interval and consume the first completed result."""
    if os.getenv("RUN_LIVE_TRANSCRIBE") != "1":
        pytest.skip("set RUN_LIVE_TRANSCRIBE=1 to create a real transcription job")

    base_url = _required_environment_variable("LIVE_TRANSCRIBE_BASE_URL")
    api_key = SecretStr(_required_environment_variable("LIVE_TRANSCRIBE_API_KEY"))
    audio_path = Path(_required_environment_variable("LIVE_TRANSCRIBE_AUDIO_PATH"))

    with httpx.Client() as http_client:
        client = TranscriptionClient(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
            timeout=30.0,
        )
        submission = client.submit(audio_path)
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            snapshot = client.get_job(submission.job_uuid)
            if snapshot.status is JobStatus.COMPLETED:
                assert snapshot.transcription
                return
            if snapshot.status is JobStatus.FAILED:
                pytest.fail("the live transcription job reached a terminal failure")
            time.sleep(POLL_INTERVAL_SECONDS)

    pytest.fail("the live transcription job exceeded its five-minute deadline")


def _required_environment_variable(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        pytest.fail(f"{name} is required when RUN_LIVE_TRANSCRIBE=1")
    return value
