"""Composition contracts for real and synthetic Graph backends."""

from pathlib import Path
from typing import cast
from uuid import UUID

import httpx2
import pytest
from pydantic import SecretStr

from glpi_ticketing_worker_publisher.composition import (
    PublisherConfigurationError,
    build_publisher_worker,
)
from glpi_ticketing_worker_publisher.graph import (
    GraphClient,
    SyntheticGraphTokenProvider,
)
from glpi_ticketing_worker_publisher.publisher import PublisherWorker
from glpi_ticketing_worker_publisher.settings import (
    PublisherGraphAuthMode,
    PublisherSettings,
)

MAILBOX_ID = UUID("00000000-0000-4000-8000-000000000003")
SYNTHETIC_GRAPH_BASE_URL = "http://provider-simulator:8080/v1.0"


def _synthetic_settings(audio_root: Path) -> PublisherSettings:
    return PublisherSettings(
        publisher_graph_auth_mode=PublisherGraphAuthMode.SYNTHETIC,
        publisher_graph_base_url=SYNTHETIC_GRAPH_BASE_URL,
        publisher_mailbox_id=MAILBOX_ID,
        database_url=SecretStr(
            "postgresql+psycopg://publisher:synthetic@database/jobs"
        ),
        audio_root=audio_root,
    )


def test_composition_injects_synthetic_token_and_graph_base_url(
    tmp_path: Path,
) -> None:
    with httpx2.Client() as http_client:
        worker = build_publisher_worker(
            _synthetic_settings(tmp_path),
            http_client=http_client,
            session_factory=cast(object, lambda: None),
        )

    assert isinstance(worker, PublisherWorker)
    graph = cast(GraphClient, worker._graph)  # noqa: SLF001 - composition contract
    assert isinstance(graph._token_provider, SyntheticGraphTokenProvider)  # noqa: SLF001
    assert str(graph._base_url) == SYNTHETIC_GRAPH_BASE_URL  # noqa: SLF001


def test_composition_fails_closed_when_certificate_settings_are_incomplete(
    tmp_path: Path,
) -> None:
    malformed_settings = _synthetic_settings(tmp_path).model_copy(
        update={
            "publisher_graph_auth_mode": PublisherGraphAuthMode.ENTRA_CERTIFICATE,
        }
    )

    with (
        httpx2.Client() as http_client,
        pytest.raises(PublisherConfigurationError),
    ):
        build_publisher_worker(
            malformed_settings,
            http_client=http_client,
            session_factory=cast(object, lambda: None),
        )
