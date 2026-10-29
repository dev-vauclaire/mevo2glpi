from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient

from glpi_ticketing_microsoft_graph_simulator.scenarios import MAX_SEED_AUDIO_BYTES
from glpi_ticketing_microsoft_graph_simulator.settings import (
    MicrosoftGraphSimulatorSettings,
)
from glpi_ticketing_microsoft_graph_simulator.web import create_app

MAILBOX_ID = UUID("10000000-0000-4000-8000-000000000001")
ADMIN_TOKEN = "synthetic-admin-token"
GRAPH_TOKEN = "synthetic-token"
RECEIVED_AT = datetime(2026, 10, 26, 8, 30, tzinfo=UTC)
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncClient:
    settings = MicrosoftGraphSimulatorSettings(
        microsoft_graph_simulator_host="127.0.0.1",
        microsoft_graph_simulator_port=8080,
        microsoft_graph_simulator_mailbox_id=MAILBOX_ID,
        microsoft_graph_simulator_admin_token=ADMIN_TOKEN,
        _env_file=None,
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings)),
        base_url="http://microsoft-graph-simulator:8080",
    ) as value:
        yield value


def _graph_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GRAPH_TOKEN}",
        "Prefer": 'IdType="ImmutableId"',
        "client-request-id": str(uuid4()),
        "return-client-request-id": "true",
    }


async def _seed(
    client: AsyncClient,
    *,
    scenario: str,
    received_at: datetime = RECEIVED_AT,
) -> dict[str, object]:
    response = await client.post(
        "/__admin/messages",
        headers={"X-Admin-Token": ADMIN_TOKEN},
        json={"scenario": scenario, "received_at": received_at.isoformat()},
    )
    assert response.status_code == 201
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


async def test_health_requires_no_secret(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_admin_seed_requires_token_without_echoing_it(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/__admin/messages",
        headers={"X-Admin-Token": "wrong-admin-token"},
        json={"scenario": "valid-wav"},
    )

    assert response.status_code == 401
    assert ADMIN_TOKEN not in response.text
    assert "wrong-admin-token" not in response.text


@pytest.mark.parametrize(
    ("scenario", "expected_extension", "expected_media_type", "expected_magic"),
    [
        ("valid-wav", "wav", "audio/wav", b"RIFF"),
        ("valid-ogg", "ogg", "audio/ogg", b"OggS"),
    ],
)
async def test_graph_lists_and_downloads_seeded_audio(
    client: AsyncClient,
    scenario: str,
    expected_extension: str,
    expected_media_type: str,
    expected_magic: bytes,
) -> None:
    seeded = await _seed(client, scenario=scenario)
    messages_response = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/mailFolders/inbox/messages",
        headers=_graph_headers(),
    )

    assert messages_response.status_code == 200
    messages = messages_response.json()["value"]
    assert [message["id"] for message in messages] == [seeded["message_id"]]
    assert messages[0]["receivedDateTime"] == "2026-10-26T08:30:00Z"

    message_id = messages[0]["id"]
    attachments_response = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{message_id}/attachments",
        headers=_graph_headers(),
    )
    attachment = attachments_response.json()["value"][0]
    download_response = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{message_id}/attachments/"
        f"{attachment['id']}/$value",
        headers=_graph_headers(),
    )

    assert seeded["scenario"] == scenario
    assert seeded["attachment_count"] == 1
    assert attachment["name"].endswith(f".{expected_extension}")
    assert attachment["contentType"] == expected_media_type
    assert download_response.headers["content-type"] == expected_media_type
    assert download_response.content.startswith(expected_magic)
    assert attachment["size"] == len(download_response.content)


@pytest.mark.parametrize(
    (
        "scenario",
        "expected_has_attachments",
        "expected_count",
        "expected_inline",
        "expected_types",
    ),
    [
        ("no-attachment", False, 0, [], []),
        (
            "two-audio-attachments",
            True,
            2,
            [False, False],
            [
                "#microsoft.graph.fileAttachment",
                "#microsoft.graph.fileAttachment",
            ],
        ),
        (
            "valid-audio-with-inline",
            True,
            2,
            [False, True],
            [
                "#microsoft.graph.fileAttachment",
                "#microsoft.graph.fileAttachment",
            ],
        ),
        (
            "non-file-attachment",
            True,
            1,
            [False],
            ["#microsoft.graph.itemAttachment"],
        ),
    ],
)
async def test_graph_exposes_scenario_attachment_topology(
    client: AsyncClient,
    scenario: str,
    expected_has_attachments: bool,
    expected_count: int,
    expected_inline: list[bool],
    expected_types: list[str],
) -> None:
    seeded = await _seed(client, scenario=scenario)
    messages_response = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/mailFolders/inbox/messages",
        headers=_graph_headers(),
    )
    message = messages_response.json()["value"][0]
    attachments_response = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{message['id']}/attachments",
        headers=_graph_headers(),
    )
    attachments = attachments_response.json()["value"]

    assert seeded["attachment_count"] == expected_count
    assert message["hasAttachments"] is expected_has_attachments
    assert [item["isInline"] for item in attachments] == expected_inline
    assert [item["@odata.type"] for item in attachments] == expected_types


async def test_graph_exposes_oversized_metadata_without_a_large_fixture(
    client: AsyncClient,
) -> None:
    seeded = await _seed(client, scenario="too-large")
    response = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{seeded['message_id']}/attachments",
        headers=_graph_headers(),
    )

    assert response.json()["value"][0]["size"] == MAX_SEED_AUDIO_BYTES + 1


async def test_non_file_attachment_cannot_be_downloaded(
    client: AsyncClient,
) -> None:
    seeded = await _seed(client, scenario="non-file-attachment")
    attachments_response = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{seeded['message_id']}/attachments",
        headers=_graph_headers(),
    )
    attachment_id = attachments_response.json()["value"][0]["id"]

    response = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{seeded['message_id']}/attachments/"
        f"{attachment_id}/$value",
        headers=_graph_headers(),
    )

    assert response.status_code == 404


async def test_admin_upload_exposes_exact_operator_audio_under_a_neutral_name(
    client: AsyncClient,
) -> None:
    operator_audio = b"OggS-operator-provided-test-audio"
    upload = await client.post(
        "/__admin/messages/audio",
        headers={
            "X-Admin-Token": ADMIN_TOKEN,
            "X-Audio-Extension": "ogg",
            "X-Received-At": "2026-10-26T10:30:00+02:00",
            "Content-Type": "audio/ogg",
        },
        content=operator_audio,
    )
    seeded = upload.json()
    attachments_response = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{seeded['message_id']}/attachments",
        headers=_graph_headers(),
    )
    attachment = attachments_response.json()["value"][0]
    download = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{seeded['message_id']}/attachments/"
        f"{attachment['id']}/$value",
        headers=_graph_headers(),
    )

    assert upload.status_code == 201
    assert seeded["scenario"] is None
    assert seeded["received_at"] == "2026-10-26T08:30:00Z"
    assert seeded["attachment_names"] == ["operator-audio.ogg"]
    assert attachment["contentType"] == "audio/ogg"
    assert download.content == operator_audio


@pytest.mark.parametrize(
    ("headers", "content", "expected_status", "expected_detail"),
    [
        (
            {"X-Admin-Token": "wrong", "X-Audio-Extension": "wav"},
            b"RIFF-test",
            401,
            "Unauthorized",
        ),
        (
            {"X-Admin-Token": ADMIN_TOKEN, "X-Audio-Extension": "mp3"},
            b"unsupported",
            422,
            "audio_extension_invalid",
        ),
        (
            {"X-Admin-Token": ADMIN_TOKEN, "X-Audio-Extension": "wav"},
            b"",
            422,
            "audio_empty",
        ),
        (
            {
                "X-Admin-Token": ADMIN_TOKEN,
                "X-Audio-Extension": "wav",
                "Content-Length": str(MAX_SEED_AUDIO_BYTES + 1),
            },
            b"x",
            413,
            "audio_too_large",
        ),
    ],
)
async def test_admin_upload_rejects_unauthorized_or_invalid_inputs(
    client: AsyncClient,
    headers: dict[str, str],
    content: bytes,
    expected_status: int,
    expected_detail: str,
) -> None:
    response = await client.post(
        "/__admin/messages/audio",
        headers=headers,
        content=content,
    )

    assert response.status_code == expected_status
    assert expected_detail in response.text


async def test_graph_paginates_messages_oldest_first(client: AsyncClient) -> None:
    newer = await _seed(
        client,
        scenario="valid-wav",
        received_at=RECEIVED_AT + timedelta(minutes=1),
    )
    older = await _seed(client, scenario="valid-ogg", received_at=RECEIVED_AT)

    first_page = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/mailFolders/inbox/messages?$top=1",
        headers=_graph_headers(),
    )
    next_link = first_page.json()["@odata.nextLink"]
    second_page = await client.get(next_link, headers=_graph_headers())

    assert first_page.json()["value"][0]["id"] == older["message_id"]
    assert second_page.json()["value"][0]["id"] == newer["message_id"]
    assert "@odata.nextLink" not in second_page.json()


async def test_graph_move_preserves_identifier_and_reset_clears_state(
    client: AsyncClient,
) -> None:
    seeded = await _seed(client, scenario="valid-wav")
    folder_response = await client.post(
        f"/v1.0/users/{MAILBOX_ID}/mailFolders",
        headers=_graph_headers(),
        json={"displayName": "Processed"},
    )
    folder_id = folder_response.json()["id"]
    moved_response = await client.post(
        f"/v1.0/users/{MAILBOX_ID}/messages/{seeded['message_id']}/move",
        headers=_graph_headers(),
        json={"destinationId": folder_id},
    )

    assert moved_response.status_code == 201
    assert moved_response.json() == {
        "id": seeded["message_id"],
        "parentFolderId": folder_id,
    }

    reset_response = await client.post(
        "/__admin/reset",
        headers={"X-Admin-Token": ADMIN_TOKEN},
    )
    location_response = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/messages/{seeded['message_id']}",
        headers=_graph_headers(),
    )

    assert reset_response.status_code == 204
    assert location_response.status_code == 404
    assert location_response.json() == {"error": {"code": "NotFound"}}


async def test_graph_requires_immutable_id_preference(client: AsyncClient) -> None:
    headers = _graph_headers()
    headers.pop("Prefer")

    response = await client.get(
        f"/v1.0/users/{MAILBOX_ID}/mailFolders/inbox/messages",
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MissingImmutableIdPreference"
