"use strict";

import { getObservability } from "./api.js";

const EXPECTED_SERVICES = [
  "worker_publisher",
  "worker_automation",
  "worker_retention",
];

const SERVICE_LABELS = new Map([
  ["worker_publisher", "Worker de publication"],
  ["worker_automation", "Worker d’automatisation"],
  ["worker_retention", "Worker de rétention"],
]);

const HEARTBEAT_LABELS = new Map([
  ["absent", "Aucun heartbeat reçu"],
  ["fresh", "Heartbeat récent"],
  ["stale", "Heartbeat en retard"],
]);

const RUNTIME_LABELS = new Map([
  ["starting", "Démarrage"],
  ["running", "En fonctionnement"],
  ["degraded", "Dégradé"],
  ["blocked", "Bloqué"],
  ["stopped", "Arrêté"],
  ["absent", "Absent"],
]);

const ALERT_LABELS = new Map([
  ["worker_absent", "Worker absent"],
  ["worker_stopped", "Worker arrêté"],
  ["worker_stale", "Worker potentiellement non réactif"],
  ["worker_blocked", "Worker bloqué"],
  ["worker_degraded", "Worker en mode dégradé"],
  ["job_blocked", "Traitement bloqué"],
  ["job_failed", "Traitement en échec"],
  ["job_pending_overdue", "Traitement en attente depuis trop longtemps"],
  ["job_retry_overdue", "Nouvelle tentative de traitement en retard"],
  ["retention_deadline_overdue", "Deadline de suppression audio dépassée"],
  ["retention_glpi_check_blocked", "Contrôle GLPI bloqué"],
  ["retention_audio_deletion_blocked", "Suppression audio bloquée"],
  ["retention_glpi_retry_overdue", "Nouvelle tentative du contrôle GLPI en retard"],
  [
    "retention_audio_deletion_retry_overdue",
    "Nouvelle tentative de suppression audio en retard",
  ],
]);

const ALERT_FILTERS = new Map([
  ["job_blocked", "blocked"],
  ["job_failed", "failed"],
  ["job_pending_overdue", "pending"],
  ["job_retry_overdue", "retry_scheduled"],
]);

const SEVERITY_LABELS = new Map([
  ["warning", "Attention"],
  ["critical", "Critique"],
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

function appendMeta(container, label, value) {
  const row = document.createElement("p");
  row.className = "worker-card__meta";

  const term = document.createElement("span");
  term.textContent = label;
  const description = document.createElement("span");
  description.textContent = textOrDash(value);

  row.append(term, description);
  container.append(row);
}

function createTextButton(label, onClick) {
  const button = document.createElement("button");
  button.className = "button button--text alert-item__action";
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function createWorkerCard(worker, onFilterJobs) {
  const heartbeatStatus = worker.heartbeat_status ?? "absent";
  const runtimeState = worker.runtime_state ?? "absent";
  const card = document.createElement("article");
  card.className = "worker-card";
  card.dataset.serviceName = textOrDash(worker.service_name);
  card.dataset.heartbeatStatus = heartbeatStatus;

  const heading = document.createElement("div");
  heading.className = "worker-card__heading";

  const title = document.createElement("h3");
  title.textContent =
    SERVICE_LABELS.get(worker.service_name) ?? textOrDash(worker.service_name);

  const runtimeBadge = document.createElement("span");
  runtimeBadge.className = "runtime-badge";
  runtimeBadge.dataset.state = runtimeState;
  runtimeBadge.textContent = RUNTIME_LABELS.get(runtimeState) ?? textOrDash(runtimeState);
  heading.append(title, runtimeBadge);
  card.append(heading);

  appendMeta(
    card,
    HEARTBEAT_LABELS.get(heartbeatStatus) ?? "Heartbeat",
    formatDate(worker.last_heartbeat_at),
  );

  if (worker.current_job_uuid) {
    const currentJob = document.createElement("p");
    currentJob.className = "worker-card__meta";
    const label = document.createElement("span");
    label.textContent = "Traitement courant";
    currentJob.append(
      label,
      createTextButton(textOrDash(worker.current_job_uuid), () =>
        onFilterJobs({ audioUuid: worker.current_job_uuid }),
      ),
    );
    card.append(currentJob);
  } else if (worker.service_name !== "worker_publisher") {
    appendMeta(card, "Traitement courant", null);
  }

  if (worker.current_ingestion_uuid) {
    appendMeta(card, "Ingestion mail courante", worker.current_ingestion_uuid);
  } else if (worker.service_name === "worker_publisher") {
    appendMeta(card, "Ingestion mail courante", null);
  }

  const errorParts = [worker.last_error_category, worker.last_error_code].filter(
    (part) => part !== null && part !== undefined && part !== "",
  );
  if (errorParts.length > 0) {
    const error = document.createElement("p");
    error.className = "worker-card__error";
    error.textContent = `Dernière erreur : ${errorParts.join(" — ")}`;
    card.append(error);
  }

  return card;
}

function orderedWorkers(workers) {
  const byService = new Map(workers.map((worker) => [worker.service_name, worker]));
  const expected = EXPECTED_SERVICES.map(
    (serviceName) =>
      byService.get(serviceName) ?? {
        service_name: serviceName,
        heartbeat_status: "absent",
        runtime_state: null,
        last_heartbeat_at: null,
        current_job_uuid: null,
        current_ingestion_uuid: null,
        last_error_category: null,
        last_error_code: null,
      },
  );
  const additional = workers.filter(
    (worker) => !EXPECTED_SERVICES.includes(worker.service_name),
  );
  return [...expected, ...additional];
}

function createAlertItem(alert, onFilterJobs) {
  const item = document.createElement("li");
  item.className = "alert-item";
  item.dataset.alertKey = textOrDash(alert.key);
  item.dataset.severity = textOrDash(alert.severity);

  const heading = document.createElement("div");
  heading.className = "alert-item__heading";
  const title = document.createElement("p");
  title.className = "alert-item__title";
  title.textContent = ALERT_LABELS.get(alert.kind) ?? textOrDash(alert.kind);
  const severity = document.createElement("span");
  severity.className = "alert-item__severity";
  severity.textContent = SEVERITY_LABELS.get(alert.severity) ?? textOrDash(alert.severity);
  heading.append(title, severity);

  const meta = document.createElement("p");
  meta.className = "alert-item__meta";
  const count = Number.isInteger(alert.count) && alert.count > 0 ? alert.count : 1;
  const countLabel = `${count} élément${count > 1 ? "s" : ""} concerné${count > 1 ? "s" : ""}`;
  const sinceLabel = alert.condition_since
    ? `condition active depuis le ${formatDate(alert.condition_since)}`
    : "condition active";
  meta.textContent = `${countLabel} — ${sinceLabel}`;

  item.append(heading, meta);

  const filterState = ALERT_FILTERS.get(alert.kind);
  if (filterState) {
    item.append(
      createTextButton("Voir les traitements concernés", () =>
        onFilterJobs({ state: filterState }),
      ),
    );
  }

  return item;
}

export function createObservabilityController({
  feedback,
  workerCards,
  alertList,
  alertEmpty,
  alertCount,
  onFilterJobs = () => {},
}) {
  let activeRequest = null;

  function render(snapshot) {
    const workers = Array.isArray(snapshot.workers) ? snapshot.workers : [];
    const alerts = Array.isArray(snapshot.alerts) ? snapshot.alerts : [];

    workerCards.replaceChildren(
      ...orderedWorkers(workers).map((worker) =>
        createWorkerCard(worker, onFilterJobs),
      ),
    );
    workerCards.setAttribute("aria-busy", "false");

    alertList.replaceChildren(
      ...alerts.map((alert) => createAlertItem(alert, onFilterJobs)),
    );
    alertEmpty.hidden = alerts.length > 0;
    alertCount.hidden = alerts.length === 0;
    alertCount.textContent = `${alerts.length} alerte${alerts.length > 1 ? "s" : ""}`;
    const alertSummary =
      alerts.length === 0
        ? "Aucune alerte active."
        : `${alerts.length} alerte${alerts.length > 1 ? "s" : ""} active${alerts.length > 1 ? "s" : ""}.`;
    feedback.textContent = `État observé le ${formatDate(snapshot.observed_at)}. ${alertSummary}`;
  }

  function refresh({ announce = true } = {}) {
    if (activeRequest !== null) {
      return activeRequest;
    }
    if (announce) {
      feedback.textContent = "Actualisation de l’état des workers…";
    }
    workerCards.setAttribute("aria-busy", "true");

    activeRequest = getObservability()
      .then(render)
      .catch((error) => {
        feedback.textContent =
          error instanceof Error
            ? error.message
            : "Impossible de charger l’état du service.";
        workerCards.setAttribute("aria-busy", "false");
      })
      .finally(() => {
        activeRequest = null;
      });
    return activeRequest;
  }

  return { refresh };
}
