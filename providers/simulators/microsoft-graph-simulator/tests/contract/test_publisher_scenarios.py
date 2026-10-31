"""Keep staging scenarios aligned with the publisher's validation policy."""

from __future__ import annotations

from io import BytesIO

import pytest

from glpi_ticketing_microsoft_graph_simulator.scenarios import (
    SeedScenario,
    build_scenario_attachments,
)
from glpi_ticketing_worker_publisher.graph import (
    AttachmentId,
    GraphAttachment,
    GraphAttachmentKind,
)
from glpi_ticketing_worker_publisher.publisher.audio import (
    inspect_audio_structure,
    select_audio_attachment,
)

MAXIMUM_ATTACHMENT_BYTES = 25 * 1024 * 1024


@pytest.mark.parametrize(
    ("scenario", "expected_failure_code"),
    [
        (SeedScenario.VALID_WAV, None),
        (SeedScenario.VALID_OGG, None),
        (SeedScenario.VALID_WAV_ALAW, None),
        (SeedScenario.VALID_WAV_MULAW, None),
        (SeedScenario.NO_ATTACHMENT, "audio_attachment_missing"),
        (SeedScenario.TWO_AUDIO_ATTACHMENTS, "attachment_count_invalid"),
        (SeedScenario.EMPTY_AUDIO, "audio_empty"),
        (SeedScenario.UNSUPPORTED_FORMAT, "audio_format_not_supported"),
        (SeedScenario.CORRUPT_WAV, "wav_header_invalid"),
        (SeedScenario.CORRUPT_OGG, "ogg_header_invalid"),
        (SeedScenario.TOO_LARGE, "audio_too_large"),
        (SeedScenario.VALID_AUDIO_WITH_INLINE, None),
        (SeedScenario.NON_FILE_ATTACHMENT, "attachment_kind_invalid"),
        (SeedScenario.SILENT_WAV, None),
    ],
)
def test_scenario_matches_publisher_validation_policy(
    scenario: SeedScenario,
    expected_failure_code: str | None,
) -> None:
    templates = build_scenario_attachments(scenario)
    attachments = tuple(
        GraphAttachment(
            attachment_id=AttachmentId(f"attachment-{index}"),
            name=template.name,
            content_type=template.media_type,
            size_bytes=template.size_bytes,
            is_inline=template.is_inline,
            kind=GraphAttachmentKind(template.kind.value),
        )
        for index, template in enumerate(templates)
    )

    selection = select_audio_attachment(
        attachments,
        maximum_bytes=MAXIMUM_ATTACHMENT_BYTES,
    )
    failure = selection.failure
    if failure is None:
        assert selection.attachment is not None
        assert selection.audio_extension is not None
        template = next(item for item in templates if not item.is_inline)
        failure = inspect_audio_structure(
            BytesIO(template.content),
            audio_extension=selection.audio_extension,
            size_bytes=len(template.content),
        )

    assert (None if failure is None else failure.technical_code) == (
        expected_failure_code
    )
