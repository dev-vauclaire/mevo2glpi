"""Static HTTP contracts for lifecycle routes and pagination validation."""

from fastapi import FastAPI

from mevo2glpi_web.routes.jobs import create_jobs_router


def test_job_routes_expose_read_only_collection_and_operator_retry() -> None:
    router = create_jobs_router(object())  # type: ignore[arg-type]
    route_methods = {
        (route.path, method)
        for route in router.routes
        for method in (route.methods or set())
    }

    assert ("/api/jobs", "POST") not in route_methods
    assert ("/api/jobs", "GET") in route_methods
    assert ("/api/jobs/{audio_uuid}", "GET") in route_methods
    assert ("/api/jobs/{audio_uuid}/events", "GET") in route_methods
    assert ("/api/jobs/{audio_uuid}/retry", "POST") in route_methods
    assert ("/audios/{audio_uuid}", "GET") in route_methods
    assert ("/audios", "POST") not in route_methods


def test_list_and_event_page_limits_are_bounded_by_http_contract() -> None:
    app = FastAPI()
    app.include_router(create_jobs_router(object()))  # type: ignore[arg-type]
    paths = app.openapi()["paths"]

    for route in ("/api/jobs", "/api/jobs/{audio_uuid}/events"):
        parameters = {
            parameter["name"]: parameter
            for parameter in paths[route]["get"]["parameters"]
        }
        assert parameters["limit"]["schema"] == {
            "type": "integer",
            "maximum": 100,
            "minimum": 1,
            "default": 25,
            "title": "Limit",
        }
        cursor_schema = parameters["cursor"]["schema"]["anyOf"][0]
        assert cursor_schema["type"] == "string"
        assert cursor_schema["minLength"] == 1
        assert cursor_schema["maxLength"] == 512
