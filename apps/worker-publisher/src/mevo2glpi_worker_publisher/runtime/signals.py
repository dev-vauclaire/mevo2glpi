"""Cooperative operating-system signal handling."""

from __future__ import annotations

import signal
from collections.abc import Generator
from contextlib import contextmanager
from threading import Event
from types import FrameType


@contextmanager
def shutdown_signal_handlers(stop_event: Event) -> Generator[None]:
    """Turn SIGINT and SIGTERM into cooperative shutdown requests."""

    def request_shutdown(_signal_number: int, _frame: FrameType | None) -> None:
        stop_event.set()

    handled_signals = (signal.SIGINT, signal.SIGTERM)
    previous_handlers = {
        handled_signal: signal.signal(handled_signal, request_shutdown)
        for handled_signal in handled_signals
    }
    try:
        yield
    finally:
        for handled_signal, previous_handler in previous_handlers.items():
            signal.signal(handled_signal, previous_handler)


__all__ = ["shutdown_signal_handlers"]
