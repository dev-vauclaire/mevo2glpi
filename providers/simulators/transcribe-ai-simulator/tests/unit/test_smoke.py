from __future__ import annotations

import pytest

from mevo2glpi_transcribe_ai_simulator import smoke

API_KEY = "synthetic-transcription-key"


def test_smoke_cli_checks_pending_then_completed_without_printing_transcript(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed_urls: list[str] = []
    monkeypatch.setenv("TRANSCRIBE_AI_SIMULATOR_API_KEY", API_KEY)

    def request_json(
        url: str,
        *,
        api_key: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> object:
        del body, content_type
        assert api_key == API_KEY
        observed_urls.append(url)
        if url.endswith("createJob"):
            return {"data": {"job_uuid": "synthetic-job"}}
        status = "PENDING" if len(observed_urls) == 2 else "COMPLETED"
        return {
            "data": {
                "status": status,
                "result": {"full_text": "must-not-be-printed"},
            }
        }

    monkeypatch.setattr(smoke, "_request_json", request_json)

    exit_code = smoke.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "job_uuid=synthetic-job" in captured.out
    assert "must-not-be-printed" not in captured.out
    assert API_KEY not in captured.out


def test_smoke_cli_never_prints_api_key_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TRANSCRIBE_AI_SIMULATOR_API_KEY", API_KEY)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise OSError(API_KEY)

    monkeypatch.setattr(smoke, "_request_json", fail)

    exit_code = smoke.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert API_KEY not in captured.err
    assert "error_type=OSError" in captured.err
