"""Typed contracts for the publisher replay use case."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from mevo2glpi_database import MailIngestionReplayFilter


class ReplayMode(StrEnum):
    """Operator-selected execution mode."""

    DRY_RUN = "dry_run"
    APPLY = "apply"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplayRequest:
    """Validated selection and mutation intent supplied by the operator."""

    replay_filter: MailIngestionReplayFilter
    mode: ReplayMode

    def __post_init__(self) -> None:
        if not isinstance(self.replay_filter, MailIngestionReplayFilter):
            raise TypeError("replay_filter must be a MailIngestionReplayFilter")
        if not isinstance(self.mode, ReplayMode):
            raise TypeError("mode must be a ReplayMode")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplayReport:
    """Content-free result safe to expose in operator logs."""

    mode: ReplayMode
    selected_ingestion_uuids: tuple[UUID, ...]
    rearmed_ingestion_uuids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ReplayMode):
            raise TypeError("mode must be a ReplayMode")
        _validate_uuid_tuple(
            self.selected_ingestion_uuids,
            "selected_ingestion_uuids",
        )
        _validate_uuid_tuple(
            self.rearmed_ingestion_uuids,
            "rearmed_ingestion_uuids",
        )
        if self.mode is ReplayMode.DRY_RUN and self.rearmed_ingestion_uuids:
            raise ValueError("a dry-run report cannot contain rearmed ingestions")
        if not set(self.rearmed_ingestion_uuids).issubset(
            self.selected_ingestion_uuids
        ):
            raise ValueError("rearmed ingestions must belong to the selection")

    @property
    def selected_count(self) -> int:
        return len(self.selected_ingestion_uuids)

    @property
    def rearmed_count(self) -> int:
        return len(self.rearmed_ingestion_uuids)


def _validate_uuid_tuple(values: object, field_name: str) -> None:
    if not isinstance(values, tuple) or any(
        not isinstance(value, UUID) for value in values
    ):
        raise TypeError(f"{field_name} must be a tuple of UUID values")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")


__all__ = ["ReplayMode", "ReplayReport", "ReplayRequest"]
