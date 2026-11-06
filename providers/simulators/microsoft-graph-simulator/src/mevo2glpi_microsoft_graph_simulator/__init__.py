"""Stateful Microsoft Graph simulator reserved for staging."""

from .state import MicrosoftGraphSimulatorState
from .web import create_app

__all__ = ["MicrosoftGraphSimulatorState", "create_app"]
