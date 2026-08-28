"""FastAPI routers exposed by the web application."""

from glpi_ticketing_web.routes.health import create_health_router
from glpi_ticketing_web.routes.jobs import create_jobs_router
from glpi_ticketing_web.routes.pages import create_pages_router

__all__ = ["create_health_router", "create_jobs_router", "create_pages_router"]
