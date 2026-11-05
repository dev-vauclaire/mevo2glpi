"""Stateful loopback tests for the Microsoft Graph mailbox adapter."""

from __future__ import annotations

import io
from uuid import UUID

import httpx2 as httpx
import pytest
from pydantic import SecretStr

from glpi_ticketing_worker_publisher.graph.client import GraphClient
from glpi_ticketing_worker_publisher.graph.contracts import GraphOutcomeUnknownError
from glpi_ticketing_worker_publisher.graph.models import (
    GraphAccessToken,
    GraphOperation,
    MoveOutcome,
)

from ._fake_graph import FakeAttachment, FakeGraphServer, FakeGraphState

MAILBOX_ID = UUID("f2cc915e-dc29-4efb-92c8-05699d099b5c")


class StaticTokenProvider:
    def __init__(self) -> None:
        self.invalidations = 0

    def get_access_token(self) -> GraphAccessToken:
        return GraphAccessToken(
            value=SecretStr("synthetic-token"),
            expires_in_seconds=3600,
        )

    def invalidate(self) -> None:
        self.invalidations += 1


def new_client(server: FakeGraphServer, http_client: httpx.Client) -> GraphClient:
    return GraphClient(
        mailbox_id=MAILBOX_ID,
        http_client=http_client,
        token_provider=StaticTokenProvider(),
        timeout=2.0,
        base_url=server.base_url,
        page_size=2,
        max_attachment_bytes=25 * 1024 * 1024,
    )


def test_stateful_fake_paginates_and_streams_without_external_network() -> None:
    state = FakeGraphState(mailbox_id=str(MAILBOX_ID), download_chunk_size=3)
    wav_content = b"RIFF-synthetic-wav-content"
    state.add_message(
        "immutable-message-1",
        attachments=[
            FakeAttachment(
                attachment_id="attachment-1",
                name="voice-message.wav",
                content_type="audio/wav",
                content=wav_content,
            )
        ],
    )
    state.add_message("immutable-message-2")
    state.add_message("immutable-message-3")

    with (
        FakeGraphServer(state) as server,
        httpx.Client(trust_env=False) as http_client,
    ):
        client = new_client(server, http_client)
        messages = client.list_messages()
        attachments = client.list_attachments(messages[0].message_id)
        sink = io.BytesIO()
        download = client.download_attachment(
            messages[0].message_id,
            attachments[0].attachment_id,
            sink,
        )

    assert [str(message.message_id) for message in messages] == [
        "immutable-message-1",
        "immutable-message-2",
        "immutable-message-3",
    ]
    assert messages[0].subject == (
        "Message vocale de la part de06 78 37 43 63pourMEVO CH-VAUCLAIRE 29 34 23 12 32"
    )
    assert attachments[0].name == "voice-message.wav"
    assert download.size_bytes == len(wav_content)
    assert sink.getvalue() == wav_content
    list_requests = [
        request
        for request in state.requests
        if request.path.endswith("/mailFolders/inbox/messages")
    ]
    assert len(list_requests) == 2
    assert list_requests[1].query["$skiptoken"] == ["2"]
    assert all(
        request.headers["host"].startswith("127.0.0.1:") for request in state.requests
    )
    assert len(state.client_request_ids) == len(state.requests)


def test_stateful_fake_creates_each_acknowledgement_folder_once() -> None:
    state = FakeGraphState(mailbox_id=str(MAILBOX_ID))

    with (
        FakeGraphServer(state) as server,
        httpx.Client(trust_env=False) as http_client,
    ):
        client = new_client(server, http_client)
        processed_created = client.ensure_mail_folder("Processed")
        processed_found = client.ensure_mail_folder("Processed")
        rejected_created = client.ensure_mail_folder("Rejected")
        rejected_found = client.ensure_mail_folder("Rejected")

    assert processed_created == processed_found
    assert rejected_created == rejected_found
    assert processed_created.folder_id != rejected_created.folder_id
    create_requests = [
        request
        for request in state.requests
        if request.method == "POST" and request.path.endswith("/mailFolders")
    ]
    assert len(create_requests) == 2
    assert sorted(state.folders.values()) == ["Inbox", "Processed", "Rejected"]


def test_stateful_fake_paginates_folder_discovery_before_creating() -> None:
    state = FakeGraphState(
        mailbox_id=str(MAILBOX_ID),
        folders={
            "folder-a": "Inbox",
            "folder-b": "Unrelated",
            "folder-z": "Processed",
        },
    )

    with (
        FakeGraphServer(state) as server,
        httpx.Client(trust_env=False) as http_client,
    ):
        client = new_client(server, http_client)
        processed = client.ensure_mail_folder("Processed")

    assert processed.folder_id == "folder-z"
    folder_gets = [
        request
        for request in state.requests
        if request.method == "GET" and request.path.endswith("/mailFolders")
    ]
    assert len(folder_gets) == 2
    assert folder_gets[1].query["$skiptoken"] == ["2"]
    assert not any(request.method == "POST" for request in state.requests)


def test_move_reconciles_a_lost_response_when_graph_remains_reachable() -> None:
    state = FakeGraphState(mailbox_id=str(MAILBOX_ID))
    message = state.add_message("immutable-message-reconciled")

    with (
        FakeGraphServer(state) as server,
        httpx.Client(trust_env=False) as http_client,
    ):
        client = new_client(server, http_client)
        processed = client.ensure_mail_folder("Processed")
        state.lose_next_move_response = True

        result = client.ensure_message_in_folder(
            message.message_id,
            processed.folder_id,
        )

    assert result.message_id == message.message_id
    assert result.parent_folder_id == processed.folder_id
    assert result.outcome is MoveOutcome.MOVED
    assert state.messages[str(message.message_id)].parent_folder_id == str(
        processed.folder_id
    )


def test_restart_reconciles_a_move_applied_before_its_response_was_lost() -> None:
    state = FakeGraphState(mailbox_id=str(MAILBOX_ID))
    message = state.add_message("immutable-message-to-move")

    with (
        FakeGraphServer(state) as server,
        httpx.Client(trust_env=False) as http_client,
    ):
        client = new_client(server, http_client)
        processed = client.ensure_mail_folder("Processed")
        state.lose_next_move_response = True
        state.lose_reconciliation_response_after_move = True

        with pytest.raises(GraphOutcomeUnknownError) as captured_error:
            client.ensure_message_in_folder(message.message_id, processed.folder_id)

        assert captured_error.value.operation is GraphOperation.MOVE_MESSAGE
        assert captured_error.value.outcome_unknown is True
        assert state.messages[str(message.message_id)].parent_folder_id == str(
            processed.folder_id
        )

        move_result_after_restart = client.ensure_message_in_folder(
            message.message_id,
            processed.folder_id,
        )

    assert move_result_after_restart.message_id == message.message_id
    assert move_result_after_restart.parent_folder_id == processed.folder_id
    assert move_result_after_restart.outcome is MoveOutcome.ALREADY_IN_DESTINATION
    move_posts = [
        request
        for request in state.requests
        if request.method == "POST" and request.path.endswith("/move")
    ]
    assert len(move_posts) == 1
