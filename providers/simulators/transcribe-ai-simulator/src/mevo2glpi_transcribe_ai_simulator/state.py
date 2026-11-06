"""Thread-safe ephemeral state for the Transcribe AI staging contract."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from uuid import UUID, uuid4


@dataclass(slots=True)
class SyntheticTranscriptionJob:
    """Polling state for one deterministic transcription result."""

    job_uuid: UUID
    poll_count: int = 0


class TranscribeAiSimulatorState:
    """Own mutable transcription state without persistence across restarts."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._jobs: dict[UUID, SyntheticTranscriptionJob] = {}

    def create_job(self) -> SyntheticTranscriptionJob:
        """Create an in-memory polling job."""
        job = SyntheticTranscriptionJob(job_uuid=uuid4())
        with self._lock:
            self._jobs[job.job_uuid] = job
        return job

    def poll_job(
        self,
        job_uuid: UUID,
    ) -> tuple[SyntheticTranscriptionJob, bool] | None:
        """Return PENDING once, then mark every later read as completed."""
        with self._lock:
            job = self._jobs.get(job_uuid)
            if job is None:
                return None
            job.poll_count += 1
            return job, job.poll_count >= 2


__all__ = ["SyntheticTranscriptionJob", "TranscribeAiSimulatorState"]
