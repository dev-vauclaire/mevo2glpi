"""HTTP translation for the deployment identity."""

from fastapi import Response

from mevo2glpi_web.schemas import VersionResponse


class VersionController:
    """Expose immutable, non-sensitive release metadata."""

    def __init__(self, version: VersionResponse) -> None:
        if not isinstance(version, VersionResponse):
            raise TypeError("version must be a VersionResponse")
        self._version = version

    def get_version(self, response: Response) -> VersionResponse:
        """Return the deployment identity without allowing intermediary caches."""
        response.headers["Cache-Control"] = "no-store"
        return self._version


__all__ = ["VersionController"]
