"""Architecture boundaries for the retention-worker application."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "glpi_ticketing_worker_retention"
)
RETENTION_ROOT = PACKAGE_ROOT / "retention"

_REPOSITORY_TYPES = {
    "AudioRetentionRepository",
    "TicketCreationJobEventRepository",
    "TicketCreationJobRepository",
    "WorkerRuntimeStatusRepository",
}


def _syntax_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(_syntax_tree(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_syntax_tree(path)):
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def test_application_remains_a_process_boundary() -> None:
    modules = _imported_modules(PACKAGE_ROOT / "application.py")

    assert not modules.intersection(
        {
            "httpx",
            "sqlalchemy",
            "glpi_ticketing_common",
            "glpi_ticketing_database",
            "glpi_ticketing_glpi",
            "retention.lifecycle",
            "retention.service",
            "retention.stages",
        }
    )


def test_only_lifecycle_imports_sqlalchemy_repositories() -> None:
    for path in RETENTION_ROOT.rglob("*.py"):
        if path.name == "lifecycle.py":
            continue
        modules = _imported_modules(path)
        imported_names = _imported_names(path)

        assert not any(
            module == "sqlalchemy" or module.startswith("sqlalchemy.")
            for module in modules
        ), path
        assert imported_names.isdisjoint(_REPOSITORY_TYPES), path


def test_service_orchestrates_ports_and_stages_without_concrete_adapters() -> None:
    service_path = RETENTION_ROOT / "service.py"
    modules = _imported_modules(service_path)
    imported_names = _imported_names(service_path)

    assert "stages" in modules
    assert "ports" in modules
    assert "GlpiClient" not in imported_names
    assert "purge_audio" not in imported_names
    assert "create_database_engine" not in imported_names
    assert not any(
        module == "httpx" or module.startswith("httpx.") for module in modules
    )


def test_runtime_context_alone_owns_process_resources() -> None:
    resource_names = {
        "create_database_engine",
        "try_acquire_advisory_lock",
    }
    importing_files = {
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if _imported_names(path).intersection(resource_names)
    }

    assert importing_files == {"runtime/context.py", "runtime/healthcheck.py"}


def test_retention_worker_never_imports_automation_application() -> None:
    for path in PACKAGE_ROOT.rglob("*.py"):
        assert not any(
            module == "glpi_ticketing_worker_automation"
            or module.startswith("glpi_ticketing_worker_automation.")
            for module in _imported_modules(path)
        ), path


def test_run_once_port_has_one_canonical_definition() -> None:
    protocol_definitions: list[tuple[str, str]] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        for node in _syntax_tree(path).body:
            if not isinstance(node, ast.ClassDef):
                continue
            is_protocol = any(
                isinstance(base, ast.Name) and base.id == "Protocol"
                for base in node.bases
            )
            exposes_run_once = any(
                isinstance(item, ast.FunctionDef) and item.name == "run_once"
                for item in node.body
            )
            if is_protocol and exposes_run_once:
                protocol_definitions.append(
                    (path.relative_to(PACKAGE_ROOT).as_posix(), node.name)
                )

    assert protocol_definitions == [("retention/ports.py", "RetentionWorkerProtocol")]
