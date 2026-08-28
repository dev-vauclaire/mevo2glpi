"""Guard the dependency direction of the layered web application."""

from __future__ import annotations

import ast
from pathlib import Path

from glpi_ticketing_web.schemas import JobStage, JobState

WEB_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "glpi_ticketing_web"


def test_flat_http_and_service_modules_were_removed() -> None:
    assert not (WEB_PACKAGE / "jobs.py").exists()
    assert not (WEB_PACKAGE / "schemas.py").exists()


def test_routes_only_depend_on_http_contracts_and_controllers() -> None:
    _assert_no_import_prefix(
        WEB_PACKAGE / "routes",
        {
            "glpi_ticketing_common",
            "glpi_ticketing_database",
            "glpi_ticketing_web.services",
            "services",
            "sqlalchemy",
        },
    )


def test_controllers_do_not_depend_on_persistence_or_storage() -> None:
    _assert_no_import_prefix(
        WEB_PACKAGE / "controllers",
        {
            "glpi_ticketing_common",
            "glpi_ticketing_database",
            "sqlalchemy",
        },
    )


def test_services_are_independent_from_the_http_framework() -> None:
    _assert_no_import_prefix(
        WEB_PACKAGE / "services",
        {
            "fastapi",
            "jinja2",
            "starlette",
            "glpi_ticketing_web.controllers",
            "glpi_ticketing_web.routes",
            "glpi_ticketing_web.schemas",
            "controllers",
            "routes",
            "schemas",
        },
    )


def test_schemas_contain_no_orm_dependency() -> None:
    _assert_no_import_prefix(
        WEB_PACKAGE / "schemas",
        {
            "glpi_ticketing_database",
            "sqlalchemy",
            "glpi_ticketing_web.controllers",
            "glpi_ticketing_web.routes",
            "glpi_ticketing_web.services",
            "controllers",
            "routes",
            "services",
        },
    )


def test_http_job_enums_are_an_explicit_stable_contract() -> None:
    assert {state.value for state in JobState} == {
        "pending",
        "processing",
        "retry_scheduled",
        "completed",
        "failed",
        "blocked",
    }
    assert {stage.value for stage in JobStage} == {
        "transcription",
        "glpi_creation",
        "finalization",
    }


def _assert_no_import_prefix(directory: Path, forbidden_prefixes: set[str]) -> None:
    violations: list[str] = []
    for source_path in sorted(directory.rglob("*.py")):
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        for node in ast.walk(tree):
            imported_modules = _imported_modules(node)
            for imported_module in imported_modules:
                if any(
                    imported_module == prefix
                    or imported_module.startswith(f"{prefix}.")
                    for prefix in forbidden_prefixes
                ):
                    violations.append(
                        f"{source_path.relative_to(directory)}: {imported_module}"
                    )

    assert not violations, "Forbidden layer imports: " + ", ".join(violations)


def _imported_modules(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.module is not None:
            return (node.module,)
        return tuple(alias.name for alias in node.names)
    return ()
