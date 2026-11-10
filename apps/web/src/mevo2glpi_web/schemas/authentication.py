"""Validated HTTP form contracts for web authentication."""

from pydantic import BaseModel, SecretStr


class LocalLoginForm(BaseModel):
    """Local-login form received from the browser.

    The credential fields deliberately have no Pydantic length constraint:
    validation errors include the rejected input and could otherwise reflect a
    password or CSRF token in a ``422`` response. The authentication boundary
    applies bounded, generic validation instead.
    """

    username: str
    password: SecretStr
    csrf_token: str
    next_path: str = "/"


class LogoutForm(BaseModel):
    """CSRF-protected logout form."""

    csrf_token: str


__all__ = ["LocalLoginForm", "LogoutForm"]
