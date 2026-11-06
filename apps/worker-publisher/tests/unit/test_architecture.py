"""Architecture boundaries for the publisher Graph adapter."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "mevo2glpi_worker_publisher"
)
GRAPH_ROOT = PACKAGE_ROOT / "graph"

_FORBIDDEN_GRAPH_DEPENDENCIES = {
    "mevo2glpi_common",
    "mevo2glpi_database",
    "mevo2glpi_glpi",
    "mevo2glpi_web",
    "mevo2glpi_worker_automation",
    "mevo2glpi_worker_retention",
    "sqlalchemy",
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


def _imports_prefix(modules: set[str], prefix: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for module in modules
    )


def test_graph_adapter_does_not_import_shared_packages_or_other_apps() -> None:
    violations: list[str] = []
    for source_path in sorted(GRAPH_ROOT.rglob("*.py")):
        modules = _imported_modules(source_path)
        for prefix in sorted(_FORBIDDEN_GRAPH_DEPENDENCIES):
            if _imports_prefix(modules, prefix):
                violations.append(
                    f"{source_path.relative_to(PACKAGE_ROOT).as_posix()}: {prefix}"
                )

    assert not violations, "Forbidden Graph imports: " + ", ".join(violations)


def test_only_graph_authentication_imports_msal() -> None:
    importing_files = {
        source_path.relative_to(PACKAGE_ROOT).as_posix()
        for source_path in PACKAGE_ROOT.rglob("*.py")
        if _imports_prefix(_imported_modules(source_path), "msal")
    }

    assert importing_files == {"graph/auth.py"}


def test_graph_client_uses_httpx2_and_never_httpx() -> None:
    client_path = GRAPH_ROOT / "client.py"

    assert client_path.is_file()
    modules = _imported_modules(client_path)
    assert _imports_prefix(modules, "httpx2")
    assert not _imports_prefix(modules, "httpx")
