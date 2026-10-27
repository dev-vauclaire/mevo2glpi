from __future__ import annotations

import json

import pytest

from glpi_ticketing_provider_simulator import admin

ADMIN_TOKEN = "synthetic-admin-token"


def test_seed_cli_calls_the_loopback_admin_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[tuple[str, str, object]] = []
    monkeypatch.setenv("PROVIDER_SIMULATOR_ADMIN_TOKEN", ADMIN_TOKEN)

    def post_json(url: str, *, token: str, payload: object) -> object:
        observed.append((url, token, payload))
        return {
            "message_id": "message-synthetic",
            "attachment_name": "synthetic-message.ogg",
            "received_at": "2026-10-26T08:30:00Z",
        }

    monkeypatch.setattr(admin, "_post_json", post_json)

    exit_code = admin.main(
        [
            "seed",
            "--extension",
            "ogg",
            "--received-at",
            "2026-10-26T10:30:00+02:00",
        ]
    )

    assert exit_code == 0
    assert observed == [
        (
            "http://127.0.0.1:8080/__admin/messages",
            ADMIN_TOKEN,
            {"extension": "ogg", "received_at": "2026-10-26T08:30:00Z"},
        )
    ]
    output = json.loads(capsys.readouterr().out)
    assert output["message_id"] == "message-synthetic"
    assert ADMIN_TOKEN not in str(output)


def test_reset_cli_never_prints_admin_token_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PROVIDER_SIMULATOR_ADMIN_TOKEN", ADMIN_TOKEN)

    def fail(_url: str, *, token: str, payload: object) -> object:
        del token, payload
        raise OSError(ADMIN_TOKEN)

    monkeypatch.setattr(admin, "_post_json", fail)

    exit_code = admin.main(["reset"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert ADMIN_TOKEN not in captured.err
    assert "error_type=OSError" in captured.err
