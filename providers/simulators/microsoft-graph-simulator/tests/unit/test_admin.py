from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from mevo2glpi_microsoft_graph_simulator import admin
from mevo2glpi_microsoft_graph_simulator.audio import SyntheticAudioExtension
from mevo2glpi_microsoft_graph_simulator.scenarios import SeedScenario

ADMIN_TOKEN = "synthetic-admin-token"


def test_seed_scenario_cli_calls_the_loopback_admin_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[tuple[str, str, object]] = []
    monkeypatch.setenv("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN", ADMIN_TOKEN)

    def post_json(url: str, *, token: str, payload: object) -> object:
        observed.append((url, token, payload))
        return {
            "message_id": "message-synthetic",
            "scenario": "valid-ogg",
            "attachment_names": ["audio.ogg"],
            "attachment_count": 1,
            "received_at": "2026-10-26T08:30:00Z",
        }

    monkeypatch.setattr(admin, "_post_json", post_json)

    exit_code = admin.main(
        [
            "seed",
            "--scenario",
            "valid-ogg",
            "--received-at",
            "2026-10-26T10:30:00+02:00",
        ]
    )

    assert exit_code == 0
    assert observed == [
        (
            "http://127.0.0.1:8080/__admin/messages",
            ADMIN_TOKEN,
            {"scenario": "valid-ogg", "received_at": "2026-10-26T08:30:00Z"},
        )
    ]
    output = json.loads(capsys.readouterr().out)
    assert output["message_id"] == "message-synthetic"
    assert ADMIN_TOKEN not in str(output)


@pytest.mark.parametrize(
    ("suffix", "expected_extension"),
    [
        (".wav", SyntheticAudioExtension.WAV),
        (".OGG", SyntheticAudioExtension.OGG),
    ],
)
def test_seed_audio_path_uploads_exact_bytes_without_disclosing_the_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    suffix: str,
    expected_extension: SyntheticAudioExtension,
) -> None:
    private_marker = "operator-private-filename"
    audio_path = tmp_path / f"{private_marker}{suffix}"
    audio_path.write_bytes(b"synthetic-operator-audio")
    observed: list[tuple[str, str, SyntheticAudioExtension, bytes, str | None]] = []
    monkeypatch.setenv("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN", ADMIN_TOKEN)

    def post_audio(
        url: str,
        *,
        token: str,
        extension: SyntheticAudioExtension,
        content: bytes,
        received_at: str | None,
    ) -> object:
        observed.append((url, token, extension, content, received_at))
        return {
            "message_id": "message-uploaded",
            "scenario": None,
            "attachment_names": [f"operator-audio.{extension.value}"],
            "attachment_count": 1,
            "received_at": "2026-10-26T08:30:00Z",
        }

    monkeypatch.setattr(admin, "_post_audio", post_audio)

    exit_code = admin.main(
        [
            "seed",
            "--audio-path",
            str(audio_path),
            "--received-at",
            "2026-10-26T08:30:00Z",
        ]
    )

    assert exit_code == 0
    assert observed == [
        (
            "http://127.0.0.1:8080/__admin/messages/audio",
            ADMIN_TOKEN,
            expected_extension,
            b"synthetic-operator-audio",
            "2026-10-26T08:30:00Z",
        )
    ]
    captured = capsys.readouterr()
    assert private_marker not in captured.out
    assert private_marker not in captured.err


def test_list_scenarios_requires_no_admin_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN", raising=False)

    exit_code = admin.main(["list-scenarios"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in payload][:4] == [
        "valid-wav",
        "valid-ogg",
        "valid-wav-alaw",
        "valid-wav-mulaw",
    ]


def test_seed_suite_injects_every_scenario_once_and_prints_manifest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[tuple[str, str, dict[str, str]]] = []
    monkeypatch.setenv("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN", ADMIN_TOKEN)

    def post_json(
        url: str,
        *,
        token: str,
        payload: dict[str, str],
    ) -> object:
        observed.append((url, token, payload))
        scenario = payload["scenario"]
        return {
            "message_id": f"message-{scenario}",
            "scenario": scenario,
            "attachment_names": [f"{scenario}.synthetic"],
            "attachment_count": 1,
            "received_at": payload["received_at"],
        }

    monkeypatch.setattr(admin, "_post_json", post_json)

    exit_code = admin.main(["seed-suite"])

    assert exit_code == 0
    expected_scenarios = [scenario.value for scenario in SeedScenario]
    assert [item[2]["scenario"] for item in observed] == expected_scenarios
    assert all(
        item[0] == "http://127.0.0.1:8080/__admin/messages" and item[1] == ADMIN_TOKEN
        for item in observed
    )
    received_at = [
        datetime.fromisoformat(item[2]["received_at"].replace("Z", "+00:00"))
        for item in observed
    ]
    assert received_at == sorted(received_at)
    assert len(set(received_at)) == len(expected_scenarios)

    captured = capsys.readouterr()
    manifest = json.loads(captured.out)
    assert manifest["complete"] is True
    assert manifest["scenario_count"] == len(expected_scenarios)
    assert manifest["seeded_count"] == len(expected_scenarios)
    assert [item["scenario"] for item in manifest["messages"]] == (expected_scenarios)
    assert ADMIN_TOKEN not in captured.out
    assert ADMIN_TOKEN not in captured.err


def test_seed_suite_prints_partial_manifest_without_secret_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN", ADMIN_TOKEN)
    successful_scenario = next(iter(SeedScenario))
    call_count = 0

    def post_json(
        _url: str,
        *,
        token: str,
        payload: dict[str, str],
    ) -> object:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise OSError(token)
        return {
            "message_id": "message-first",
            "scenario": successful_scenario.value,
            "attachment_names": ["audio.wav"],
            "attachment_count": 1,
            "received_at": payload["received_at"],
        }

    monkeypatch.setattr(admin, "_post_json", post_json)

    exit_code = admin.main(["seed-suite"])

    captured = capsys.readouterr()
    manifest = json.loads(captured.out)
    assert exit_code == 1
    assert manifest["complete"] is False
    assert manifest["scenario_count"] == len(SeedScenario)
    assert manifest["seeded_count"] == 1
    assert manifest["messages"][0]["scenario"] == successful_scenario.value
    assert "error_type=OSError" in captured.err
    assert ADMIN_TOKEN not in captured.out
    assert ADMIN_TOKEN not in captured.err


@pytest.mark.parametrize(
    "arguments",
    [
        ["seed", "--extension", "wav"],
        ["seed", "--scenario", "valid-wav", "--audio-path", "audio.wav"],
    ],
)
def test_removed_or_conflicting_seed_options_are_rejected(
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        admin.main(arguments)

    assert raised.value.code == 2


def test_invalid_audio_path_does_not_disclose_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "private-patient-marker"
    missing = tmp_path / f"{private_marker}.wav"
    monkeypatch.setenv("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN", ADMIN_TOKEN)

    exit_code = admin.main(["seed", "--audio-path", str(missing)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert private_marker not in captured.err
    assert private_marker not in captured.out


@pytest.mark.parametrize(
    ("suffix", "content"),
    [
        (".mp3", b"unsupported"),
        (".wav", b""),
        (".wav", b"too-large"),
    ],
)
def test_invalid_audio_path_is_rejected_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    content: bytes,
) -> None:
    audio_path = tmp_path / f"audio{suffix}"
    audio_path.write_bytes(content)
    monkeypatch.setenv("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(admin, "MAX_SEED_AUDIO_BYTES", 4)

    def unexpected_request(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid audio must not reach the simulator")

    monkeypatch.setattr(admin, "_post_audio", unexpected_request)

    assert admin.main(["seed", "--audio-path", str(audio_path)]) == 1


def test_reset_cli_never_prints_admin_token_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN", ADMIN_TOKEN)

    def fail(_url: str, *, token: str, payload: object) -> object:
        del token, payload
        raise OSError(ADMIN_TOKEN)

    monkeypatch.setattr(admin, "_post_json", fail)

    exit_code = admin.main(["reset"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert ADMIN_TOKEN not in captured.err
    assert "error_type=OSError" in captured.err
