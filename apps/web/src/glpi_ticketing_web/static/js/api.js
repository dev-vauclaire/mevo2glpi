"use strict";

const DEFAULT_ERROR = "Le service est momentanément indisponible.";

const STATUS_MESSAGES = new Map([
  [400, "La demande envoyée n’est pas valide."],
  [404, "Le traitement demandé est introuvable."],
  [409, "L’état actuel du traitement ne permet pas cette opération."],
  [410, "L’audio de ce traitement a été supprimé."],
  [413, "Le fichier dépasse la taille maximale autorisée."],
  [415, "Ce format audio n’est pas pris en charge."],
  [422, "Le fichier ou les paramètres fournis ne sont pas valides."],
  [503, "Le service est temporairement indisponible. Réessayez plus tard."],
]);

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function requestJson(path, options = {}) {
  let response;

  try {
    response = await fetch(path, {
      ...options,
      cache: "no-store",
      headers: { Accept: "application/json", ...options.headers },
    });
  } catch (_error) {
    throw new ApiError(0, DEFAULT_ERROR);
  }

  if (!response.ok) {
    throw new ApiError(
      response.status,
      STATUS_MESSAGES.get(response.status) ?? DEFAULT_ERROR,
    );
  }

  if (response.status === 204) {
    return null;
  }

  try {
    return await response.json();
  } catch (_error) {
    throw new ApiError(0, DEFAULT_ERROR);
  }
}

function jobPath(audioUuid, suffix = "") {
  return `/api/jobs/${encodeURIComponent(audioUuid)}${suffix}`;
}

export function uploadAudio(file) {
  const body = new FormData();
  body.append("audio_file", file, file.name);
  return requestJson("/api/jobs", { method: "POST", body });
}

export function listJobs({ limit = 25, state = "", audioUuid = "", cursor = "" }) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (state) {
    query.set("state", state);
  }
  if (audioUuid) {
    query.set("audio_uuid", audioUuid);
  }
  if (cursor) {
    query.set("cursor", cursor);
  }
  return requestJson(`/api/jobs?${query.toString()}`);
}

export function getJob(audioUuid) {
  return requestJson(jobPath(audioUuid));
}

export function listJobEvents(audioUuid, { limit = 25, cursor = "" } = {}) {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) {
    query.set("cursor", cursor);
  }
  return requestJson(`${jobPath(audioUuid, "/events")}?${query.toString()}`);
}

export function retryJob(audioUuid) {
  return requestJson(jobPath(audioUuid, "/retry"), { method: "POST" });
}

export function getObservability() {
  return requestJson("/api/observability");
}
