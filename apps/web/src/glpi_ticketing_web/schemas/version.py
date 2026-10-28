"""Public deployment identity exposed by the web application."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class VersionResponse(BaseModel):
    """Non-sensitive release metadata shared with operators."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Literal["development", "staging", "production"]
    release: str
    commit_sha: str


__all__ = ["VersionResponse"]
