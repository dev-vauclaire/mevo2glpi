"""Controller for server-rendered pages."""

from fastapi import Request, Response
from fastapi.templating import Jinja2Templates


class PageController:
    """Build responses for the web user interface."""

    def __init__(
        self,
        *,
        templates: Jinja2Templates,
    ) -> None:
        self._templates = templates

    def index(self, request: Request) -> Response:
        """Render the read-only operations dashboard."""
        return self._templates.TemplateResponse(
            request=request,
            name="index.html",
        )


__all__ = ["PageController"]
