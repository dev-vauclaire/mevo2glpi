"""Transcription client contract against the disposable Transcribe AI stack."""

import time

import httpx
import pytest

from glpi_ticketing_worker_automation.transcription import (
    JobStatus,
    LegacyTranscriptionClient,
)

pytestmark = [pytest.mark.transcribe_ai, pytest.mark.timeout(900)]

POLL_INTERVAL_SECONDS = 2.0
JOB_TIMEOUT_SECONDS = 10 * 60.0


def test_transcription_client_completes_containerized_flow(
    transcribe_ai_environment,
) -> None:
    """Submit an audio and poll the real API until its first completed result."""
    with httpx.Client() as http_client:
        client = LegacyTranscriptionClient(
            base_url=transcribe_ai_environment.base_url,
            api_key=transcribe_ai_environment.api_key,
            http_client=http_client,
            timeout=30.0,
        )
        submission = client.submit(transcribe_ai_environment.audio_path)
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            snapshot = client.get_job(submission.job_uuid)
            if snapshot.status is JobStatus.COMPLETED:
                assert snapshot.transcription is not None
                assert bool(snapshot.transcription.strip())
                return
            if snapshot.status is JobStatus.FAILED:
                pytest.fail("The containerized transcription job failed")
            time.sleep(POLL_INTERVAL_SECONDS)

    pytest.fail("The containerized transcription job exceeded its deadline")
