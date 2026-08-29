"use strict";

import { getJob, listJobEvents, listJobs, retryJob } from "./api.js";

const PAGE_SIZE = 25;

const STATE_LABELS = new Map([
  ["pending", "En attente"],
  ["processing", "En cours"],
  ["retry_scheduled", "Nouvelle tentative planifiée"],
  ["completed", "Terminé"],
  ["failed", "Échec"],
  ["blocked", "Bloqué"],
]);

const STAGE_LABELS = new Map([
  ["transcription", "Transcription"],
  ["glpi_creation", "Création GLPI"],
  ["finalization", "Finalisation"],
]);

const RETENTION_LABELS = new Map([
  ["inactive", "Inactive"],
  ["scheduled", "Planifiée"],
  ["checking", "Vérification GLPI"],
  ["retry_scheduled", "Nouvelle tentative planifiée"],
  ["deleting", "Suppression en cours"],
  ["deleted", "Audio supprimé"],
  ["blocked", "Bloquée"],
]);

const EVENT_LABELS = new Map([
  ["job_created", "Traitement créé"],
  ["processing_started", "Traitement démarré"],
  ["processing_resumed", "Traitement repris"],
  ["transcription_submitted", "Transcription soumise"],
  ["transcription_succeeded", "Transcription validée"],
  ["retry_scheduled", "Nouvelle tentative planifiée"],
  ["ticket_confirmed", "Ticket GLPI confirmé"],
  ["job_completed", "Traitement terminé"],
  ["job_failed", "Traitement en échec"],
  ["job_blocked", "Traitement bloqué"],
  ["operator_retry_requested", "Relance opérateur demandée"],
  ["retention_retry_scheduled", "Nouvelle tentative de rétention planifiée"],
  ["retention_blocked", "Rétention bloquée"],
  ["audio_deletion_started", "Suppression de l’audio démarrée"],
  ["audio_deleted", "Audio supprimé"],
]);

const ACTOR_LABELS = new Map([
  ["web", "Interface web"],
  ["worker_automation", "Worker d’automatisation"],
  ["worker_retention", "Worker de rétention"],
]);

function textOrDash(value) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function formatDate(value) {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? "—"
    : new Intl.DateTimeFormat("fr-FR", {
        dateStyle: "short",
        timeStyle: "medium",
      }).format(date);
}

function formatBytes(value) {
  if (!Number.isFinite(value)) {
    return "—";
  }
  return new Intl.NumberFormat("fr-FR", {
    style: "unit",
    unit: "byte",
    unitDisplay: "long",
  }).format(value);
}

function stateLabel(state) {
  return STATE_LABELS.get(state) ?? textOrDash(state);
}

function stageLabel(stage) {
  return STAGE_LABELS.get(stage) ?? textOrDash(stage);
}

function retentionLabel(retention) {
  return RETENTION_LABELS.get(retention) ?? textOrDash(retention);
}

function errorScopeLabel(scope) {
  if (scope === "job") {
    return "Traitement";
  }
  if (scope === "system") {
    return "Système";
  }
  return textOrDash(scope);
}

function createCell(text) {
  const cell = document.createElement("td");
  cell.textContent = text;
  return cell;
}

function createStateBadge(state) {
  const badge = document.createElement("span");
  badge.className = "status-badge";
  badge.dataset.state = state;
  badge.textContent = stateLabel(state);
  return badge;
}

function canDownload(job) {
  return (
    job.state === "completed" &&
    job.retention_status !== "deleting" &&
    job.retention_status !== "deleted"
  );
}

function canRetry(job) {
  return (
    ["failed", "blocked"].includes(job.state) &&
    job.retention_status !== "deleting" &&
    job.retention_status !== "deleted" &&
    !job.audio_deleted_at
  );
}

function createJobRow(job, onOpenDetail) {
  const row = document.createElement("tr");

  const uuidCell = document.createElement("th");
  uuidCell.scope = "row";
  uuidCell.className = "uuid-cell";
  uuidCell.textContent = textOrDash(job.audio_uuid);

  const stateCell = document.createElement("td");
  stateCell.append(createStateBadge(job.state));

  const actionCell = document.createElement("td");
  const detailButton = document.createElement("button");
  detailButton.className = "button button--text";
  detailButton.type = "button";
  detailButton.textContent = "Voir le détail";
  detailButton.setAttribute(
    "aria-label",
    `Voir le détail du traitement ${textOrDash(job.audio_uuid)}`,
  );
  detailButton.addEventListener("click", () => onOpenDetail(job.audio_uuid));
  actionCell.append(detailButton);

  row.append(
    uuidCell,
    stateCell,
    createCell(stageLabel(job.stage)),
    createCell(formatDate(job.updated_at)),
    createCell(textOrDash(job.attempt_count)),
    actionCell,
  );
  return row;
}

function appendDetailItem(container, label, value) {
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = textOrDash(value);
  container.append(term, description);
}

function eventDescription(event) {
  const parts = [EVENT_LABELS.get(event.event_type) ?? textOrDash(event.event_type)];
  parts.push(stateLabel(event.state));
  if (event.stage) {
    parts.push(stageLabel(event.stage));
  }
  return parts.filter((part) => part !== "—").join(" — ");
}

function createEventItem(event) {
  const item = document.createElement("li");
  item.className = "timeline__item";

  const heading = document.createElement("p");
  heading.className = "timeline__heading";
  heading.textContent = eventDescription(event) || "Transition";

  const date = document.createElement("time");
  date.className = "timeline__date";
  if (event.occurred_at) {
    date.dateTime = event.occurred_at;
  }
  date.textContent = formatDate(event.occurred_at);

  const actor = document.createElement("p");
  actor.className = "timeline__meta";
  actor.textContent = ACTOR_LABELS.get(event.actor) ?? textOrDash(event.actor);

  const error = document.createElement("p");
  error.className = "timeline__error";
  const errorParts = [
    errorScopeLabel(event.error_scope),
    textOrDash(event.error_category),
    textOrDash(event.technical_code),
  ].filter((part) => part !== "—");
  error.textContent = errorParts.length > 0 ? `Erreur : ${errorParts.join(" — ")}` : "";
  error.hidden = errorParts.length === 0;

  item.append(heading, actor, date, error);
  return item;
}

export function createDashboardController({
  filters,
  stateFilter,
  uuidFilter,
  feedback,
  tableContainer,
  tableBody,
  previousButton,
  nextButton,
  dialog,
  detailTitle,
  detailClose,
  detailFeedback,
  detailContainer,
  detailActions,
  audioDownload,
  retryButton,
  eventsFeedback,
  eventsList,
  eventsMore,
}) {
  let currentCursor = "";
  let nextCursor = "";
  let previousCursors = [];
  let pageRequest = 0;
  let activePageRequests = 0;
  let selectedUuid = null;
  let detailGeneration = 0;
  let eventCursor = "";
  let eventRequest = 0;
  const retryingUuids = new Set();

  function resetPagination() {
    currentCursor = "";
    nextCursor = "";
    previousCursors = [];
  }

  function currentQuery() {
    return {
      limit: PAGE_SIZE,
      state: stateFilter.value,
      audioUuid: uuidFilter.value.trim(),
      cursor: currentCursor,
    };
  }

  async function loadPage({ announce = true, onlyWhenIdle = false } = {}) {
    if (onlyWhenIdle && activePageRequests > 0) {
      return;
    }
    const requestId = pageRequest + 1;
    pageRequest = requestId;
    activePageRequests += 1;
    if (announce) {
      feedback.textContent = "Chargement des traitements…";
    }

    try {
      const page = await listJobs(currentQuery());
      if (requestId !== pageRequest) {
        return;
      }

      const items = Array.isArray(page.items) ? page.items : [];
      tableBody.replaceChildren(
        ...items.map((job) => createJobRow(job, openDetail)),
      );
      tableContainer.hidden = items.length === 0;
      feedback.textContent =
        items.length === 0
          ? "Aucun traitement ne correspond aux filtres."
          : `${items.length} traitement${items.length > 1 ? "s" : ""} affiché${items.length > 1 ? "s" : ""}.`;

      nextCursor = page.next_cursor ?? "";
      previousButton.disabled = previousCursors.length === 0;
      nextButton.disabled = !nextCursor;
    } catch (error) {
      if (requestId !== pageRequest) {
        return;
      }
      tableContainer.hidden = true;
      tableBody.replaceChildren();
      feedback.textContent =
        error instanceof Error ? error.message : "Impossible de charger les traitements.";
      nextButton.disabled = true;
    } finally {
      activePageRequests -= 1;
    }
  }

  async function loadEvents({ append = false } = {}) {
    if (selectedUuid === null) {
      return;
    }
    const requestedUuid = selectedUuid;
    const requestedGeneration = detailGeneration;
    const requestId = eventRequest + 1;
    eventRequest = requestId;
    eventsMore.disabled = true;
    eventsFeedback.textContent = "Chargement de l’historique…";

    try {
      const page = await listJobEvents(requestedUuid, {
        limit: PAGE_SIZE,
        cursor: append ? eventCursor : "",
      });
      if (
        selectedUuid !== requestedUuid ||
        detailGeneration !== requestedGeneration ||
        eventRequest !== requestId
      ) {
        return;
      }
      const items = Array.isArray(page.items) ? page.items : [];
      const eventItems = items.map(createEventItem);
      if (append) {
        eventsList.append(...eventItems);
      } else {
        eventsList.replaceChildren(...eventItems);
      }
      eventCursor = page.next_cursor ?? "";
      eventsMore.hidden = !eventCursor;
      eventsFeedback.textContent =
        eventsList.childElementCount === 0 ? "Aucun événement enregistré." : "";
    } catch (error) {
      if (
        selectedUuid !== requestedUuid ||
        detailGeneration !== requestedGeneration ||
        eventRequest !== requestId
      ) {
        return;
      }
      if (!append) {
        eventsList.replaceChildren();
      }
      eventsFeedback.textContent =
        error instanceof Error ? error.message : "Impossible de charger l’historique.";
    } finally {
      if (
        selectedUuid === requestedUuid &&
        detailGeneration === requestedGeneration &&
        eventRequest === requestId
      ) {
        eventsMore.disabled = false;
      }
    }
  }

  function renderDetail(job) {
    detailTitle.textContent = textOrDash(job.audio_uuid);
    detailContainer.replaceChildren();
    appendDetailItem(detailContainer, "État", stateLabel(job.state));
    appendDetailItem(detailContainer, "Étape", stageLabel(job.stage));
    appendDetailItem(detailContainer, "Format audio", job.audio_extension);
    appendDetailItem(detailContainer, "Taille", formatBytes(job.audio_size_bytes));
    appendDetailItem(detailContainer, "Créé le", formatDate(job.created_at));
    appendDetailItem(
      detailContainer,
      "Premier démarrage",
      formatDate(job.first_started_at),
    );
    appendDetailItem(detailContainer, "Mis à jour le", formatDate(job.updated_at));
    appendDetailItem(detailContainer, "Terminé le", formatDate(job.finished_at));
    appendDetailItem(detailContainer, "Prochaine tentative", formatDate(job.next_attempt_at));
    appendDetailItem(detailContainer, "Tentatives", job.attempt_count);
    appendDetailItem(
      detailContainer,
      "Retries consécutifs",
      job.consecutive_retry_count,
    );
    appendDetailItem(detailContainer, "Ticket GLPI", job.glpi_ticket_id);
    appendDetailItem(
      detailContainer,
      "Rétention",
      retentionLabel(job.retention_status),
    );
    appendDetailItem(
      detailContainer,
      "Audio supprimé le",
      formatDate(job.audio_deleted_at),
    );
    appendDetailItem(
      detailContainer,
      "Périmètre d’erreur",
      errorScopeLabel(job.last_error_scope),
    );
    appendDetailItem(detailContainer, "Catégorie d’erreur", job.last_error_category);
    appendDetailItem(detailContainer, "Code technique", job.last_error_code);

    detailContainer.hidden = false;
    detailActions.hidden = false;
    audioDownload.hidden = !canDownload(job);
    if (canDownload(job)) {
      audioDownload.href = `/audios/${encodeURIComponent(job.audio_uuid)}`;
    } else {
      audioDownload.removeAttribute("href");
    }
    retryButton.hidden = !canRetry(job);
    retryButton.disabled = retryingUuids.has(job.audio_uuid);
    detailFeedback.textContent = "";
  }

  async function openDetail(audioUuid) {
    const requestedGeneration = detailGeneration + 1;
    detailGeneration = requestedGeneration;
    selectedUuid = audioUuid;
    eventCursor = "";
    detailTitle.textContent = textOrDash(audioUuid);
    detailFeedback.textContent = "Chargement du détail…";
    detailContainer.hidden = true;
    detailActions.hidden = true;
    eventsList.replaceChildren();
    eventsFeedback.textContent = "";
    eventsMore.hidden = true;
    if (!dialog.open) {
      dialog.showModal();
    }

    try {
      const job = await getJob(audioUuid);
      if (
        selectedUuid !== audioUuid ||
        detailGeneration !== requestedGeneration
      ) {
        return;
      }
      renderDetail(job);
      await loadEvents();
    } catch (error) {
      if (
        selectedUuid === audioUuid &&
        detailGeneration === requestedGeneration
      ) {
        detailFeedback.textContent =
          error instanceof Error ? error.message : "Impossible de charger le détail.";
      }
    }
  }

  filters.addEventListener("submit", (event) => {
    event.preventDefault();
    resetPagination();
    loadPage();
  });

  nextButton.addEventListener("click", () => {
    if (!nextCursor) {
      return;
    }
    previousCursors.push(currentCursor);
    currentCursor = nextCursor;
    loadPage();
  });

  previousButton.addEventListener("click", () => {
    if (previousCursors.length === 0) {
      return;
    }
    currentCursor = previousCursors.pop() ?? "";
    loadPage();
  });

  detailClose.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    selectedUuid = null;
    detailGeneration += 1;
    eventRequest += 1;
  });

  eventsMore.addEventListener("click", () => loadEvents({ append: true }));

  retryButton.addEventListener("click", async () => {
    if (selectedUuid === null) {
      return;
    }
    const requestedUuid = selectedUuid;
    const requestedGeneration = detailGeneration;
    if (retryingUuids.has(requestedUuid)) {
      return;
    }
    const confirmed = window.confirm(
      "Confirmer la remise en attente de ce traitement ?",
    );
    if (!confirmed) {
      return;
    }

    retryingUuids.add(requestedUuid);
    retryButton.disabled = true;
    detailFeedback.textContent = "Relance en cours…";
    try {
      await retryJob(requestedUuid);
      if (
        selectedUuid === requestedUuid &&
        detailGeneration === requestedGeneration &&
        dialog.open
      ) {
        await openDetail(requestedUuid);
      }
      await loadPage({ announce: false });
    } catch (error) {
      if (
        selectedUuid === requestedUuid &&
        detailGeneration === requestedGeneration
      ) {
        detailFeedback.textContent =
          error instanceof Error ? error.message : "Impossible de relancer le traitement.";
      }
    } finally {
      retryingUuids.delete(requestedUuid);
      if (selectedUuid === requestedUuid) {
        retryButton.disabled = false;
      }
    }
  });

  function applyFilters({ state = "", audioUuid = "" }) {
    stateFilter.value = state;
    uuidFilter.value = audioUuid;
    resetPagination();
    return loadPage();
  }

  return {
    applyFilters,
    refresh: (options = {}) => loadPage(options),
  };
}
