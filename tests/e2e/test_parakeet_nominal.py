"""Nominal end-to-end pipeline on the pinned CPU Parakeet provider."""

from __future__ import annotations

import pytest
from tests.e2e.support import (
    SyntheticAudio,
    get_job_events,
    load_correlated_glpi_ticket,
    seed_audio_job,
    wait_for_job_state,
)
from tests.e2e.support.parakeet import (
    ParakeetComposeEnvironment,
    load_synthetic_speech,
)
from tests.support.mvp_compose import MvpComposeEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.transcription_model,
    pytest.mark.timeout(45 * 60),
]

PIPELINE_TIMEOUT_SECONDS = 20 * 60.0


def test_synthetic_ingestion_creates_glpi_ticket_through_parakeet(
    parakeet_e2e_environment: MvpComposeEnvironment,
    parakeet_environment: ParakeetComposeEnvironment,
    glpi_environment,
) -> None:
    """Qualify the nominal synchronous provider from audio bytes to GLPI."""
    environment = parakeet_e2e_environment
    audio = load_synthetic_speech()

    accepted = seed_audio_job(
        environment,
        SyntheticAudio(filename="synthetic-french-speech.wav", content=audio),
    )
    with environment.client() as client:
        completed = wait_for_job_state(
            client,
            accepted.audio_uuid,
            "completed",
            timeout_seconds=PIPELINE_TIMEOUT_SECONDS,
        )
        assert completed.stage == "finalization"
        assert completed.attempt_count == 1
        assert completed.consecutive_retry_count == 0
        assert completed.glpi_ticket_id is not None
        events = get_job_events(client, accepted.audio_uuid)
        assert any(event.event_type == "transcription_succeeded" for event in events)
        assert any(event.event_type == "ticket_confirmed" for event in events)

    ticket = load_correlated_glpi_ticket(
        glpi_environment,
        completed.audio_uuid,
        completed.glpi_ticket_id,
    )
    assert ticket.contains_non_empty_transcription()
    assert ticket.references_audio_url(
        f"{environment.base_url}/audios/{completed.audio_uuid}"
    )

    automation_networks = _network_names(environment, "worker-automation")
    assert glpi_environment.network_name in automation_networks
    assert parakeet_environment.network_name in automation_networks
    _assert_tcp_reachable(
        environment,
        "worker-automation",
        parakeet_environment.network_alias,
        5092,
    )


def _network_names(
    environment: MvpComposeEnvironment,
    service_name: str,
) -> set[str]:
    network_settings = environment.inspect_service(service_name)["NetworkSettings"]
    assert isinstance(network_settings, dict)
    networks = network_settings["Networks"]
    assert isinstance(networks, dict)
    return set(networks)


def _assert_tcp_reachable(
    environment: MvpComposeEnvironment,
    service_name: str,
    host: str,
    port: int,
) -> None:
    source = (
        "import socket; "
        f"connection=socket.create_connection(({host!r}, {port}), timeout=3); "
        "connection.close()"
    )
    environment.compose(
        "exec",
        "-T",
        service_name,
        "python",
        "-c",
        source,
    )
