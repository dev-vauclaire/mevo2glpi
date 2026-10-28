"""Static contracts for the one-shot migration runtime image."""

import tomllib
from pathlib import Path


def test_migration_image_context_contains_only_required_workspace_sources() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    dockerignore_rules = set(
        (repository_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )
    dockerfile = (
        repository_root / "infra/docker/apps/database-migrations/Dockerfile"
    ).read_text(encoding="utf-8")
    workspace = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    for member in workspace["tool"]["uv"]["workspace"]["members"]:
        manifest = f"{member}/pyproject.toml"
        assert f"COPY {manifest} {manifest}" in dockerfile
        assert f"!{member}/" in dockerignore_rules
        assert f"!{manifest}" in dockerignore_rules

    assert (
        "COPY apps/database-migrations/src apps/database-migrations/src" in dockerfile
    )
    assert "COPY packages/database/src packages/database/src" in dockerfile
    assert "!apps/database-migrations/src/**" in dockerignore_rules
    assert "!packages/database/src/**" in dockerignore_rules


def test_migration_runtime_is_locked_one_shot_and_non_root() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    dockerfile = (
        repository_root / "infra/docker/apps/database-migrations/Dockerfile"
    ).read_text(encoding="utf-8")
    runtime = dockerfile.split("FROM ${PYTHON_IMAGE} AS runtime", maxsplit=1)[1]

    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "USER 10001:10001" in runtime
    assert "COPY --from=builder /app/.venv /app/.venv" in runtime
    assert "/usr/local/bin/uv" not in runtime
    assert "COPY apps/" not in runtime
    assert "COPY packages/" not in runtime
    assert 'CMD ["glpi-ticketing-database-migrations"]' in runtime
    assert "HEALTHCHECK" not in runtime
