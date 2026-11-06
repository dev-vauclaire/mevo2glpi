"""Controller for the process health endpoint."""


class HealthController:
    """Expose the liveness state without invoking a business service."""

    def health(self) -> dict[str, str]:
        """Return the stable liveness response."""
        return {"status": "ok"}


__all__ = ["HealthController"]
