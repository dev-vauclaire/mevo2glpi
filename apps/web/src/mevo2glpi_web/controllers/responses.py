"""HTTP responses that own resources for their complete ASGI lifecycle."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class ManagedStreamingResponse(StreamingResponse):
    """Close an injected resource after every normal or failed ASGI outcome."""

    def __init__(self, *args, close_callback: Callable[[], None], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._close_callback = close_callback

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._close_callback()


__all__ = ["ManagedStreamingResponse"]
