"""Static contracts for the web runtime image context."""

import tomllib
from pathlib import Path


def test_web_image_context_includes_its_workspace_packages() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    dockerignore_rules = set(
        (repository_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )
    dockerfile = (repository_root / "infra/docker/web/Dockerfile").read_text(
        encoding="utf-8"
    )
    workspace = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    for member in workspace["tool"]["uv"]["workspace"]["members"]:
        manifest = f"{member}/pyproject.toml"
        assert f"COPY {manifest} {manifest}" in dockerfile
        assert f"!{member}/" in dockerignore_rules
        assert f"!{manifest}" in dockerignore_rules
    assert "COPY packages/common/src packages/common/src" in dockerfile
    assert "COPY packages/database/src packages/database/src" in dockerfile
    assert "!packages/common/src/**" in dockerignore_rules
    assert "!packages/database/src/**" in dockerignore_rules
