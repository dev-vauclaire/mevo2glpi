"""HTTP-wide concerns for the web application."""

from .authentication import WebAuthenticationMiddleware

__all__ = ["WebAuthenticationMiddleware"]
