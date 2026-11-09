"""Repository-level checks for local Markdown links."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK_PATTERN = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
ROOT_DOCUMENT_NAMES = (
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
)


def test_local_markdown_links_target_existing_files() -> None:
    failures: list[str] = []

    for document in _documentation_files():
        content = document.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK_PATTERN.findall(content):
            target = _local_target(raw_target)
            if target is None:
                continue
            resolved_target = (document.parent / target).resolve()
            if not resolved_target.is_relative_to(REPOSITORY_ROOT):
                failures.append(
                    f"{document.relative_to(REPOSITORY_ROOT)}: link escapes the "
                    f"repository: {raw_target}"
                )
            elif not resolved_target.exists():
                failures.append(
                    f"{document.relative_to(REPOSITORY_ROOT)}: missing target "
                    f"{raw_target}"
                )

    assert not failures, "\n".join(failures)


def _documentation_files() -> tuple[Path, ...]:
    documents = list((REPOSITORY_ROOT / "docs").rglob("*.md"))
    documents.extend(
        path
        for name in ROOT_DOCUMENT_NAMES
        if (path := REPOSITORY_ROOT / name).is_file()
    )
    return tuple(sorted(documents))


def _local_target(raw_target: str) -> Path | None:
    normalized_target = raw_target.strip()
    if not normalized_target or normalized_target.startswith("#"):
        return None

    # Markdown permits an optional quoted title after the URL. Repository links
    # do not contain spaces, so keeping the first token is deterministic here.
    normalized_target = normalized_target.split(maxsplit=1)[0]
    parsed_target = urlsplit(normalized_target)
    if parsed_target.scheme or parsed_target.netloc or not parsed_target.path:
        return None
    return Path(unquote(parsed_target.path))
