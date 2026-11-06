"""Architecture boundaries for the automation worker application."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "mevo2glpi_worker_automation"
)
WORKER_ROOT = PACKAGE_ROOT / "worker"

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
            "mevo2glpi_database",
            "glpi",
            "transcription",
        }
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "audio.py",
        "outcomes.py",
        "policies.py",
        "ports.py",
        "service.py",
        "stages/ticket_creation.py",
        "stages/transcription.py",
        "transitions.py",
    ],
)
def test_only_lifecycle_knows_sqlalchemy_repositories(relative_path: str) -> None:
    path = WORKER_ROOT / relative_path
    modules = _imported_modules(path)
    imported_names = _imported_names(path)

    assert not any(
        module == "sqlalchemy" or module.startswith("sqlalchemy.") for module in modules
    )
    assert imported_names.isdisjoint(_REPOSITORY_TYPES)


def test_service_depends_on_stages_instead_of_remote_adapters() -> None:
    modules = _imported_modules(WORKER_ROOT / "service.py")

    assert "stages" in modules
    assert not any(
        module == "glpi"
        or module.startswith("glpi.")
        or module == "transcription"
        or module.startswith("transcription.")
        for module in modules
    )


def test_legacy_worker_aggregate_modules_are_removed() -> None:
    assert not (WORKER_ROOT / "processor.py").exists()
    assert not (WORKER_ROOT / "models.py").exists()


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
                protocol_definitions.append((path.name, node.name))

    assert protocol_definitions == [("ports.py", "AutomationWorkerProtocol")]
