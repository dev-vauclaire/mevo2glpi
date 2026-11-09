"use strict";

import { createDashboardController } from "./dashboard.js";
import { createObservabilityController } from "./observability.js";
import { createRejectionsController } from "./rejections.js";

const REFRESH_INTERVAL_MS = 10000;
const refreshButton = document.querySelector("[data-dashboard-refresh]");
const jobsPanel = document.querySelector("[data-jobs-panel]");

const dashboard = createDashboardController({
  filters: document.querySelector("[data-job-filters]"),
  stateFilter: document.querySelector("[data-state-filter]"),
  uuidFilter: document.querySelector("[data-uuid-filter]"),
  feedback: document.querySelector("[data-dashboard-feedback]"),
  tableContainer: document.querySelector("[data-jobs-table-container]"),
  tableBody: document.querySelector("[data-jobs-table]"),
  previousButton: document.querySelector("[data-page-previous]"),
  nextButton: document.querySelector("[data-page-next]"),
  dialog: document.querySelector("[data-job-dialog]"),
  detailTitle: document.querySelector("[data-detail-title]"),
  detailClose: document.querySelector("[data-detail-close]"),
  detailFeedback: document.querySelector("[data-detail-feedback]"),
  detailContainer: document.querySelector("[data-job-detail]"),
  detailActions: document.querySelector("[data-detail-actions]"),
  audioDownload: document.querySelector("[data-audio-download]"),
  retryButton: document.querySelector("[data-job-retry]"),
  eventsFeedback: document.querySelector("[data-events-feedback]"),
  eventsList: document.querySelector("[data-job-events]"),
  eventsMore: document.querySelector("[data-events-more]"),
});

function showFilteredJobs(filters) {
  dashboard.applyFilters(filters);
  jobsPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

const observability = createObservabilityController({
  feedback: document.querySelector("[data-observability-feedback]"),
  workerCards: document.querySelector("[data-worker-cards]"),
  alertList: document.querySelector("[data-alert-list]"),
  alertEmpty: document.querySelector("[data-alert-empty]"),
  alertCount: document.querySelector("[data-alert-count]"),
  onFilterJobs: showFilteredJobs,
});

const rejections = createRejectionsController({
  feedback: document.querySelector("[data-rejections-feedback]"),
  tableContainer: document.querySelector("[data-rejections-table-container]"),
  tableBody: document.querySelector("[data-rejections-table]"),
  previousButton: document.querySelector("[data-rejections-previous]"),
  nextButton: document.querySelector("[data-rejections-next]"),
});

let refreshTimer = null;
let refreshInFlight = null;
let pollingStopped = false;

function refreshApplication({ announce = true, onlyWhenIdle = false } = {}) {
  if (refreshInFlight !== null) {
    return refreshInFlight;
  }

  refreshButton.disabled = true;
  refreshInFlight = Promise.allSettled([
    Promise.resolve().then(() => dashboard.refresh({ announce, onlyWhenIdle })),
    Promise.resolve().then(() => observability.refresh({ announce })),
    Promise.resolve().then(() => rejections.refresh({ announce, onlyWhenIdle })),
  ]).finally(() => {
    refreshInFlight = null;
    refreshButton.disabled = false;
  });
  return refreshInFlight;
}

function clearScheduledRefresh() {
  if (refreshTimer !== null) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function scheduleRefresh() {
  clearScheduledRefresh();
  if (pollingStopped || document.visibilityState !== "visible") {
    return;
  }
  refreshTimer = window.setTimeout(async () => {
    refreshTimer = null;
    await refreshApplication({ announce: false, onlyWhenIdle: true });
    scheduleRefresh();
  }, REFRESH_INTERVAL_MS);
}

function refreshThenSchedule(options) {
  clearScheduledRefresh();
  void refreshApplication(options).finally(scheduleRefresh);
}

refreshButton.addEventListener("click", () => {
  refreshThenSchedule({ announce: true, onlyWhenIdle: false });
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") {
    clearScheduledRefresh();
    return;
  }
  refreshThenSchedule({ announce: false, onlyWhenIdle: true });
});

window.addEventListener(
  "pagehide",
  () => {
    pollingStopped = true;
    clearScheduledRefresh();
  },
  { once: true },
);

refreshThenSchedule({ announce: true, onlyWhenIdle: false });
