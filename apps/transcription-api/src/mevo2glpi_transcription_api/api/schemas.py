"""Pydantic response contracts exposed by the transcription API."""

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


class TranscriptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


__all__ = ["HealthResponse", "TranscriptionResponse"]
