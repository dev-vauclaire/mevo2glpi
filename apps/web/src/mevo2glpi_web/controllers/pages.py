"""Controller for server-rendered pages."""

from fastapi import Request, Response
from fastapi.templating import Jinja2Templates

from mevo2glpi_web.schemas import VersionResponse

_ENVIRONMENT_LABELS = {
    "development": "Développement",
    "staging": "Staging",
    "production": "Production",
}


class PageController:
    """Build responses for the web user interface."""

    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        version: VersionResponse,
    ) -> None:
        if not isinstance(version, VersionResponse):
            raise TypeError("version must be a VersionResponse")
        self._templates = templates
        self._version = version

    def index(self, request: Request) -> Response:
        """Render the read-only operations dashboard."""
        return self._templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "release_environment": _ENVIRONMENT_LABELS[self._version.environment],
                "release_name": self._version.release,
                "release_commit_sha": self._version.commit_sha,
                "release_short_commit_sha": self._version.commit_sha[:12],
            },
        )


__all__ = ["PageController"]
