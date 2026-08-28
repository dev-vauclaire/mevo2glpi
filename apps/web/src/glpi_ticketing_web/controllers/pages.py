"""Controller for server-rendered pages."""

from fastapi import Request, Response
from fastapi.templating import Jinja2Templates


class PageController:
    """Build responses for the web user interface."""

    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        max_upload_bytes: int,
    ) -> None:
        self._templates = templates
        self._max_upload_bytes = max_upload_bytes

    def index(self, request: Request) -> Response:
        """Render the audio upload page."""
        return self._templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "max_upload_bytes": self._max_upload_bytes,
                "upload_path": request.app.url_path_for("upload_audio"),
            },
        )


__all__ = ["PageController"]
