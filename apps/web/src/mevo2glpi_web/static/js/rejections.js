"use strict";

import { listRejections } from "./api.js";

const PAGE_SIZE = 25;

const REJECTION_LABELS = new Map([
  ["audio_attachment_missing", "Aucune pièce jointe audio"],
  ["attachment_count_invalid", "Nombre de pièces jointes audio invalide"],
  ["attachment_kind_invalid", "Type de pièce jointe invalide"],
  ["audio_format_not_supported", "Format audio non pris en charge"],
  ["audio_empty", "Fichier audio vide"],
  ["audio_too_large", "Fichier audio trop volumineux"],
  ["wav_header_invalid", "En-tête WAV invalide"],
  ["wav_truncated", "Fichier WAV tronqué"],
  ["wav_format_invalid", "Format interne WAV invalide"],
  ["wav_structure_invalid", "Structure WAV invalide"],
  ["wav_format_missing", "Informations de format WAV absentes"],
  ["wav_data_missing", "Données audio WAV absentes"],
  ["ogg_header_invalid", "En-tête OGG invalide"],
  ["ogg_version_invalid", "Version OGG non prise en charge"],
  ["ogg_continuation_invalid", "Structure de continuation OGG invalide"],
  ["ogg_checksum_invalid", "Somme de contrôle OGG invalide"],
  ["ogg_truncated", "Fichier OGG tronqué"],
  ["mp3_header_invalid", "En-tête MP3 invalide"],
  ["mp3_truncated", "Fichier MP3 tronqué"],
  ["mp3_frame_missing", "Trame audio MP3 absente"],
  ["m4a_header_invalid", "En-tête M4A invalide"],
  ["m4a_truncated", "Fichier M4A tronqué"],
  ["m4a_file_type_missing", "Type de fichier M4A absent"],
  ["m4a_metadata_missing", "Métadonnées M4A absentes"],
  ["m4a_audio_track_missing", "Piste audio M4A absente"],
  ["m4a_media_data_missing", "Données média M4A absentes"],
]);

const CATEGORY_LABELS = new Map([
  ["invalid_attachment", "Pièce jointe invalide"],
  ["invalid_audio", "Audio invalide"],
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

function createCell(text) {
  const cell = document.createElement("td");
  cell.textContent = text;
  return cell;
}

function createReasonCell(rejection) {
  const cell = document.createElement("td");
  cell.className = "rejection-reason";

  const reason = document.createElement("span");
  reason.className = "rejection-reason__label";
  reason.textContent =
    REJECTION_LABELS.get(rejection.rejection_code) ??
    CATEGORY_LABELS.get(rejection.rejection_category) ??
    "Message rejeté";

  const code = document.createElement("code");
  code.className = "rejection-reason__code";
  code.textContent = textOrDash(rejection.rejection_code);

  cell.append(reason, code);
  return cell;
}

function createRejectionRow(rejection) {
  const row = document.createElement("tr");
  const uuidCell = document.createElement("th");
  uuidCell.scope = "row";
  uuidCell.className = "uuid-cell";
  uuidCell.textContent = textOrDash(rejection.ingestion_uuid);
  row.append(
    uuidCell,
    createCell(formatDate(rejection.received_at)),
    createCell(formatDate(rejection.rejected_at)),
    createReasonCell(rejection),
  );
  return row;
}

export function createRejectionsController({
  feedback,
  tableContainer,
  tableBody,
  previousButton,
  nextButton,
}) {
  let currentCursor = "";
  let nextCursor = "";
  let previousCursors = [];
  let pageRequest = 0;
  let activePageRequests = 0;

  async function loadPage({ announce = true, onlyWhenIdle = false } = {}) {
    if (onlyWhenIdle && activePageRequests > 0) {
      return;
    }
    const requestId = pageRequest + 1;
    pageRequest = requestId;
    activePageRequests += 1;
    if (announce) {
      feedback.textContent = "Chargement des messages rejetés…";
    }

    try {
      const page = await listRejections({
        limit: PAGE_SIZE,
        cursor: currentCursor,
      });
      if (requestId !== pageRequest) {
        return;
      }
      const items = Array.isArray(page.items) ? page.items : [];
      tableBody.replaceChildren(...items.map(createRejectionRow));
      tableContainer.hidden = items.length === 0;
      feedback.textContent =
        items.length === 0
          ? "Aucun message rejeté conservé."
          : `${items.length} message${items.length > 1 ? "s" : ""} rejeté${items.length > 1 ? "s" : ""} affiché${items.length > 1 ? "s" : ""}.`;
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
        error instanceof Error
          ? error.message
          : "Impossible de charger les messages rejetés.";
      nextButton.disabled = true;
    } finally {
      activePageRequests -= 1;
    }
  }

  nextButton.addEventListener("click", () => {
    if (!nextCursor) {
      return;
    }
    previousCursors.push(currentCursor);
    currentCursor = nextCursor;
    void loadPage();
  });

  previousButton.addEventListener("click", () => {
    if (previousCursors.length === 0) {
      return;
    }
    currentCursor = previousCursors.pop() ?? "";
    void loadPage();
  });

  return {
    refresh: (options = {}) => loadPage(options),
  };
}
