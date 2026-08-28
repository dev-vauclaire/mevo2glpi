"""Unit tests for cooperative OS signal handling."""

from __future__ import annotations

import signal
from threading import Event
from types import FrameType
from typing import cast

import pytest

from glpi_ticketing_worker_automation.runtime.signals import (
    shutdown_signal_handlers,
)


def test_shutdown_signal_handlers_set_event_and_restore_previous_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = Event()
    registered_handlers: dict[signal.Signals, object] = {}
    calls: list[tuple[signal.Signals, object]] = []

    def fake_signal(
        handled_signal: signal.Signals,
        handler: object,
    ) -> object:
        calls.append((handled_signal, handler))
        previous_handler = registered_handlers.get(
            handled_signal,
            f"previous-{handled_signal.name}",
        )
        registered_handlers[handled_signal] = handler
        return previous_handler

    monkeypatch.setattr(signal, "signal", fake_signal)

    with shutdown_signal_handlers(stop_event):
        sigterm_handler = registered_handlers[signal.SIGTERM]
        assert callable(sigterm_handler)
        sigterm_handler(signal.SIGTERM, cast(FrameType | None, None))
        assert stop_event.is_set()

    assert len(calls) == 4
    assert registered_handlers[signal.SIGINT] == "previous-SIGINT"
    assert registered_handlers[signal.SIGTERM] == "previous-SIGTERM"
