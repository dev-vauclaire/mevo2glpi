"""Static contracts for the MVP dashboard assets."""

from pathlib import Path

ASSET_ROOT = Path(__file__).resolve().parents[2] / "src" / "glpi_ticketing_web"
TEMPLATE_ROOT = ASSET_ROOT / "templates"
JAVASCRIPT_ROOT = ASSET_ROOT / "static" / "js"


def test_index_exposes_reusable_multi_audio_upload_and_dashboard() -> None:
    template = (TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'action="{{ upload_path }}"' in template
    assert 'name="audio_file"' in template
    assert "multiple" in template
    assert "data-upload-results" in template
    assert "data-jobs-table" in template
    assert "data-job-dialog" in template
    assert "data-job-events" in template
    assert "data-job-retry" in template
    assert 'type="module"' in template
    assert "path='/js/app.js'" in template


def test_upload_pool_limits_browser_concurrency_to_three() -> None:
    uploads = (JAVASCRIPT_ROOT / "uploads.js").read_text(encoding="utf-8")

    assert "MAX_CONCURRENT_UPLOADS = 3" in uploads
    assert "Math.min(MAX_CONCURRENT_UPLOADS, files.length)" in uploads
    assert "Promise.all" in uploads
    assert 'form.addEventListener("submit"' in uploads
    assert "form.reset()" in uploads


def test_dashboard_uses_cursor_and_separate_event_endpoint() -> None:
    api = (JAVASCRIPT_ROOT / "api.js").read_text(encoding="utf-8")
    dashboard = (JAVASCRIPT_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert 'query.set("cursor", cursor)' in api
    assert 'jobPath(audioUuid, "/events")' in api
    assert 'jobPath(audioUuid, "/retry")' in api
    assert 'cache: "no-store"' in api
    assert "page.next_cursor" in dashboard
    assert '["failed", "blocked"]' in dashboard
    assert 'job.state === "completed"' in dashboard
    assert "event.technical_code" in dashboard
    assert "ACTOR_LABELS" in dashboard


def test_dashboard_names_every_retention_lifecycle_event() -> None:
    dashboard = (JAVASCRIPT_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert '["retention_retry_scheduled",' in dashboard
    assert '["retention_blocked",' in dashboard
    assert '["audio_deletion_started",' in dashboard
    assert '["audio_deleted",' in dashboard


def test_dashboard_serializes_polling_and_ignores_stale_detail_requests() -> None:
    dashboard = (JAVASCRIPT_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert "setInterval" not in dashboard
    assert "window.setTimeout(pollDashboard" in dashboard
    assert "onlyWhenIdle: true" in dashboard
    assert "activePageRequests" in dashboard
    assert "detailGeneration" in dashboard
    assert "eventRequest" in dashboard


def test_dashboard_disables_impossible_retries_and_names_row_actions() -> None:
    dashboard = (JAVASCRIPT_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert 'job.retention_status !== "deleting"' in dashboard
    assert 'job.retention_status !== "deleted"' in dashboard
    assert "!job.audio_deleted_at" in dashboard
    assert 'uuidCell.scope = "row"' in dashboard
    assert '"aria-label"' in dashboard


def test_javascript_never_injects_server_content_as_html() -> None:
    javascript_sources = [
        path.read_text(encoding="utf-8")
        for path in sorted(JAVASCRIPT_ROOT.glob("*.js"))
    ]

    assert javascript_sources
    assert all("innerHTML" not in source for source in javascript_sources)
    assert any("textContent" in source for source in javascript_sources)
    assert any("replaceChildren" in source for source in javascript_sources)


def test_obsolete_confirmation_assets_are_removed() -> None:
    assert not (TEMPLATE_ROOT / "uploaded.html").exists()
    assert not (JAVASCRIPT_ROOT / "upload.js").exists()
