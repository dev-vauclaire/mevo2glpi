from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPOSITORY_ROOT = (
    Path(__file__).parents[2] / "src" / "glpi_ticketing_database" / "repositories"
)
ORM_MODELS = {
    "AudioRetention",
    "MailIngestion",
    "MailIngestionEvent",
    "TicketCreationJob",
    "TicketCreationJobEvent",
    "WorkerRuntimeStatus",
}


@pytest.mark.parametrize(
    ("module_name", "expected_model"),
    [
        ("audio_retention.py", "AudioRetention"),
        ("mail_ingestion.py", "MailIngestion"),
        ("mail_ingestion_event.py", "MailIngestionEvent"),
        ("ticket_creation_job.py", "TicketCreationJob"),
        ("ticket_creation_job_event.py", "TicketCreationJobEvent"),
        ("worker_runtime_status.py", "WorkerRuntimeStatus"),
    ],
)
def test_each_repository_module_imports_exactly_one_orm_model(
    module_name: str,
    expected_model: str,
) -> None:
    source_path = REPOSITORY_ROOT / module_name
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=module_name)
    imported_models = {
        imported.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 2
        and node.module == "models"
        for imported in node.names
        if imported.name in ORM_MODELS
    }

    assert imported_models == {expected_model}
