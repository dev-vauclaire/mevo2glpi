"""FastAPI routers exposed by the web application."""

from mevo2glpi_web.routes.health import create_health_router
from mevo2glpi_web.routes.jobs import create_jobs_router
from mevo2glpi_web.routes.observability import create_observability_router
from mevo2glpi_web.routes.pages import create_pages_router
from mevo2glpi_web.routes.version import create_version_router

__all__ = [
    "create_health_router",
    "create_jobs_router",
    "create_observability_router",
    "create_pages_router",
    "create_version_router",
]
