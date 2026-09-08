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
    assert "data-worker-cards" in template
    assert "data-alert-list" in template
    assert "data-alert-empty" in template
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


def test_dashboard_names_the_publisher_event_actor() -> None:
    dashboard = (JAVASCRIPT_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert '["worker_publisher", "Worker de publication"]' in dashboard


def test_application_serializes_polling_and_stale_detail_requests() -> None:
    app = (JAVASCRIPT_ROOT / "app.js").read_text(encoding="utf-8")
    dashboard = (JAVASCRIPT_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert "setInterval" not in app
    assert "setInterval" not in dashboard
    assert "Promise.allSettled" in app
    assert "refreshInFlight" in app
    assert "window.setTimeout" in app
    assert 'document.addEventListener("visibilitychange"' in app
    assert 'document.visibilityState !== "visible"' in app
    assert "onlyWhenIdle: true" in app
    assert "activePageRequests" in dashboard
    assert "detailGeneration" in dashboard
    assert "eventRequest" in dashboard


def test_observability_uses_the_deduplicated_contract_and_safe_dom_apis() -> None:
    api = (JAVASCRIPT_ROOT / "api.js").read_text(encoding="utf-8")
    observability = (JAVASCRIPT_ROOT / "observability.js").read_text(encoding="utf-8")

    assert 'requestJson("/api/observability")' in api
    assert "snapshot.observed_at" in observability
    assert "snapshot.workers" in observability
    assert "snapshot.alerts" in observability
    assert "worker.last_error_category" in observability
    assert "worker.last_error_code" in observability
    assert "alert.key" in observability
    assert "alert.condition_since" in observability
    assert "replaceChildren" in observability
    assert "textContent" in observability
    assert "innerHTML" not in observability


def test_observability_names_every_alert_and_links_job_alerts_to_filters() -> None:
    observability = (JAVASCRIPT_ROOT / "observability.js").read_text(encoding="utf-8")
    dashboard = (JAVASCRIPT_ROOT / "dashboard.js").read_text(encoding="utf-8")

    expected_kinds = {
        "worker_absent",
        "worker_stopped",
        "worker_stale",
        "worker_blocked",
        "worker_degraded",
        "job_blocked",
        "job_failed",
        "job_pending_overdue",
        "job_retry_overdue",
        "retention_deadline_overdue",
        "retention_glpi_check_blocked",
        "retention_audio_deletion_blocked",
        "retention_glpi_retry_overdue",
        "retention_audio_deletion_retry_overdue",
    }
    assert all(f'"{kind}"' in observability for kind in expected_kinds)
    assert 'applyFilters({ state = "", audioUuid = "" })' in dashboard
    assert "stateFilter.value = state" in dashboard
    assert "uuidFilter.value = audioUuid" in dashboard
    assert "Voir les traitements concernés" in observability


def test_observability_renders_two_expected_workers_and_an_empty_state() -> None:
    observability = (JAVASCRIPT_ROOT / "observability.js").read_text(encoding="utf-8")

    assert '"worker_automation", "worker_retention"' in observability
    assert "orderedWorkers(workers)" in observability
    assert "alertEmpty.hidden = alerts.length > 0" in observability
    assert "alertCount.hidden = alerts.length === 0" in observability


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
