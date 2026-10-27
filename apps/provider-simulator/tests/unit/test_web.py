from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from glpi_ticketing_provider_simulator.settings import ProviderSimulatorSettings
from glpi_ticketing_provider_simulator.web import SYNTHETIC_TRANSCRIPTION, create_app

MAILBOX_ID = UUID("10000000-0000-4000-8000-000000000001")
ADMIN_TOKEN = "synthetic-admin-token"
TRANSCRIPTION_API_KEY = "synthetic-transcription-key"
GRAPH_TOKEN = "synthetic-token"
RECEIVED_AT = datetime(2026, 10, 26, 8, 30, tzinfo=UTC)


@pytest.fixture
def settings() -> ProviderSimulatorSettings:
    return ProviderSimulatorSettings(
        provider_simulator_host="127.0.0.1",
        provider_simulator_port=8080,
        provider_simulator_mailbox_id=MAILBOX_ID,
        provider_simulator_admin_token=ADMIN_TOKEN,
        provider_simulator_transcription_api_key=TRANSCRIPTION_API_KEY,
        _env_file=None,
    )


@pytest.fixture
def client(settings: ProviderSimulatorSettings) -> TestClient:
    with TestClient(
        create_app(settings), base_url="http://provider-simulator:8080"
    ) as value:
        yield value


def _graph_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GRAPH_TOKEN}",
        "Prefer": 'IdType="ImmutableId"',
        "client-request-id": str(uuid4()),
        "return-client-request-id": "true",
    }


def _seed(
    client: TestClient,
    *,
    extension: str,
    received_at: datetime = RECEIVED_AT,
) -> dict[str, object]:
    response = client.post(
        "/__admin/messages",
        headers={"X-Admin-Token": ADMIN_TOKEN},
        json={
            "extension": extension,
            "received_at": received_at.isoformat(),
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def test_health_requires_no_provider_or_admin_secret(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_admin_seed_requires_token_without_echoing_it(
    client: TestClient,
) -> None:
    response = client.post(
        "/__admin/messages",
        headers={"X-Admin-Token": "wrong-admin-token"},
        json={"extension": "wav"},
    )

    assert response.status_code == 401
    assert ADMIN_TOKEN not in response.text
    assert "wrong-admin-token" not in response.text


@pytest.mark.parametrize(
    ("extension", "expected_media_type", "expected_magic"),
    [
        ("wav", "audio/wav", b"RIFF"),
        ("ogg", "audio/ogg", b"OggS"),
    ],
)
def test_graph_lists_and_downloads_seeded_audio(
    client: TestClient,
    extension: str,
    expected_media_type: str,
    expected_magic: bytes,
) -> None:
    seeded = _seed(client, extension=extension)
    messages_response = client.get(
        f"/v1.0/users/{MAILBOX_ID}/mailFolders/inbox/messages",
        headers=_graph_headers(),
    )

    assert messages_response.status_code == 200
    messages = messages_response.json()["value"]
    assert [message["id"] for message in messages] == [seeded["message_id"]]
    assert messages[0]["receivedDateTime"] == "2026-10-26T08:30:00Z"

    message_id = messages[0]["id"]
    attachments_response = client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{message_id}/attachments",
        headers=_graph_headers(),
    )
    assert attachments_response.status_code == 200
    attachment = attachments_response.json()["value"][0]
    assert attachment["name"].endswith(f".{extension}")
    assert attachment["contentType"] == expected_media_type

    download_response = client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{message_id}/attachments/"
        f"{attachment['id']}/$value",
        headers=_graph_headers(),
    )
    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == expected_media_type
    assert download_response.content.startswith(expected_magic)
    assert attachment["size"] == len(download_response.content)


def test_graph_paginates_messages_oldest_first(client: TestClient) -> None:
    newer = _seed(
        client,
        extension="wav",
        received_at=RECEIVED_AT + timedelta(minutes=1),
    )
    older = _seed(client, extension="ogg", received_at=RECEIVED_AT)

    first_page = client.get(
        f"/v1.0/users/{MAILBOX_ID}/mailFolders/inbox/messages?$top=1",
        headers=_graph_headers(),
    )
    assert first_page.status_code == 200
    assert first_page.json()["value"][0]["id"] == older["message_id"]

    next_link = first_page.json()["@odata.nextLink"]
    second_page = client.get(next_link, headers=_graph_headers())
    assert second_page.status_code == 200
    assert second_page.json()["value"][0]["id"] == newer["message_id"]
    assert "@odata.nextLink" not in second_page.json()


def test_graph_move_preserves_immutable_message_identifier(
    client: TestClient,
) -> None:
    seeded = _seed(client, extension="wav")
    folder_response = client.post(
        f"/v1.0/users/{MAILBOX_ID}/mailFolders",
        headers=_graph_headers(),
        json={"displayName": "Processed"},
    )
    assert folder_response.status_code == 201
    folder_id = folder_response.json()["id"]

    moved_response = client.post(
        f"/v1.0/users/{MAILBOX_ID}/messages/{seeded['message_id']}/move",
        headers=_graph_headers(),
        json={"destinationId": folder_id},
    )
    assert moved_response.status_code == 201
    assert moved_response.json() == {
        "id": seeded["message_id"],
        "parentFolderId": folder_id,
    }

    location_response = client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{seeded['message_id']}",
        headers=_graph_headers(),
    )
    assert location_response.status_code == 200
    assert location_response.json()["id"] == seeded["message_id"]
    assert location_response.json()["parentFolderId"] == folder_id

    inbox_response = client.get(
        f"/v1.0/users/{MAILBOX_ID}/mailFolders/inbox/messages",
        headers=_graph_headers(),
    )
    assert inbox_response.json()["value"] == []


def test_admin_reset_removes_messages_and_transcription_jobs(
    client: TestClient,
) -> None:
    _seed(client, extension="wav")
    submission = client.post(
        "/api/batchTranscription/createJob",
        headers={"X-API-KEY": TRANSCRIPTION_API_KEY},
        files={"audioFile": ("audio.wav", b"synthetic", "audio/wav")},
    )
    job_uuid = submission.json()["data"]["job_uuid"]

    reset_response = client.post(
        "/__admin/reset",
        headers={"X-Admin-Token": ADMIN_TOKEN},
    )

    assert reset_response.status_code == 204
    inbox_response = client.get(
        f"/v1.0/users/{MAILBOX_ID}/mailFolders/inbox/messages",
        headers=_graph_headers(),
    )
    assert inbox_response.json()["value"] == []
    result_response = client.get(
        "/api/batchTranscription/result",
        params={"job_uuid": job_uuid},
        headers={"X-API-KEY": TRANSCRIPTION_API_KEY},
    )
    assert result_response.status_code == 404


def test_transcription_contract_returns_pending_then_completed_without_logging_data(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        submission = client.post(
            "/api/batchTranscription/createJob",
            headers={"X-API-KEY": TRANSCRIPTION_API_KEY},
            files={"audioFile": ("audio.ogg", b"OggS-synthetic", "audio/ogg")},
        )
        assert submission.status_code == 200
        job_uuid = submission.json()["data"]["job_uuid"]

        pending = client.get(
            "/api/batchTranscription/result",
            params={"job_uuid": job_uuid},
            headers={"X-API-KEY": TRANSCRIPTION_API_KEY},
        )
        completed = client.get(
            "/api/batchTranscription/result",
            params={"job_uuid": job_uuid},
            headers={"X-API-KEY": TRANSCRIPTION_API_KEY},
        )

    assert pending.status_code == 200
    assert pending.json()["data"] == {
        "job_id": job_uuid,
        "status": "PENDING",
    }
    assert completed.status_code == 200
    assert completed.json()["data"]["status"] == "COMPLETED"
    assert completed.json()["data"]["result"]["full_text"] == (SYNTHETIC_TRANSCRIPTION)
    assert TRANSCRIPTION_API_KEY not in caplog.text
    assert SYNTHETIC_TRANSCRIPTION not in caplog.text


def test_transcription_rejects_wrong_api_key_without_echoing_it(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/batchTranscription/createJob",
        headers={"X-API-KEY": "wrong-transcription-key"},
        files={"audioFile": ("audio.wav", b"synthetic", "audio/wav")},
    )

    assert response.status_code == 401
    assert TRANSCRIPTION_API_KEY not in response.text
    assert "wrong-transcription-key" not in response.text
