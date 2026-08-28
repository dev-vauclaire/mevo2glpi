"use strict";

import { createDashboardController } from "./dashboard.js";
import { createUploadController } from "./uploads.js";

const dashboard = createDashboardController({
  filters: document.querySelector("[data-job-filters]"),
  stateFilter: document.querySelector("[data-state-filter]"),
  uuidFilter: document.querySelector("[data-uuid-filter]"),
  refreshButton: document.querySelector("[data-dashboard-refresh]"),
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

createUploadController({
  form: document.querySelector("[data-upload-form]"),
  fileInput: document.querySelector("[data-audio-input]"),
  selectedFiles: document.querySelector("[data-selected-files]"),
  resultsContainer: document.querySelector("[data-upload-results-container]"),
  results: document.querySelector("[data-upload-results]"),
  feedback: document.querySelector("[data-upload-feedback]"),
  submitButton: document.querySelector("[data-upload-submit]"),
  onAccepted: () => dashboard.refresh(),
});
