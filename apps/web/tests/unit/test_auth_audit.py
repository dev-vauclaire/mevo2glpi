"""Security contracts for sanitized authentication logs."""

from __future__ import annotations

import logging

import pytest

from mevo2glpi_web.auth.audit import AuthAuditEvent, LoggingAuthAuditRecorder
from mevo2glpi_web.auth.models import AuthMode


def test_authentication_log_contains_only_bounded_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_username = "synthetic.private.operator"
    private_password = "synthetic-private-password"
    recorder = LoggingAuthAuditRecorder()

    with caplog.at_level(logging.WARNING, logger="mevo2glpi_web.authentication"):
        recorder.record(
            AuthAuditEvent.LOGIN_FAILED,
            auth_mode=AuthMode.LOCAL,
            succeeded=False,
            category="authentication",
            technical_code="credentials_invalid",
        )

    assert "event_type=login_failed" in caplog.text
    assert "auth_mode=local" in caplog.text
    assert "outcome=failure" in caplog.text
    assert "category=authentication" in caplog.text
    assert "technical_code=credentials_invalid" in caplog.text
    assert private_username not in caplog.text
    assert private_password not in caplog.text


def test_successful_authentication_is_logged_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = LoggingAuthAuditRecorder()

    with caplog.at_level(logging.INFO, logger="mevo2glpi_web.authentication"):
        recorder.record(
            AuthAuditEvent.LOGIN_SUCCEEDED,
            auth_mode=AuthMode.OIDC,
            succeeded=True,
        )

    record = caplog.records[-1]
    assert record.levelno == logging.INFO
    assert "event_type=login_succeeded" in record.getMessage()
    assert "auth_mode=oidc" in record.getMessage()


def test_unrecognized_diagnostics_are_never_written_to_authentication_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_value = "synthetic-private-operator-identifier"
    recorder = LoggingAuthAuditRecorder()

    with caplog.at_level(logging.WARNING, logger="mevo2glpi_web.authentication"):
        recorder.record(
            AuthAuditEvent.LOGIN_FAILED,
            auth_mode=AuthMode.LOCAL,
            succeeded=False,
            category=private_value,
            technical_code=private_value,
        )

    assert private_value not in caplog.text
    assert "category=none" in caplog.text
    assert "technical_code=none" in caplog.text
