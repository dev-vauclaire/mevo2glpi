"use strict";

import { uploadAudio } from "./api.js";

export const MAX_CONCURRENT_UPLOADS = 3;

function setResult(resultElement, state, message) {
  resultElement.dataset.state = state;
  const status = resultElement.querySelector("[data-upload-status]");
  if (status !== null) {
    status.textContent = message;
  }
}

function createResult(file) {
  const item = document.createElement("li");
  item.className = "upload-list__item";
  item.dataset.state = "queued";

  const filename = document.createElement("span");
  filename.className = "upload-list__filename";
  filename.textContent = file.name;

  const status = document.createElement("span");
  status.className = "upload-list__status";
  status.dataset.uploadStatus = "";
  status.textContent = "En attente";

  item.append(filename, status);
  return item;
}

async function processUpload(file, resultElement, onAccepted) {
  setResult(resultElement, "uploading", "Envoi en cours…");

  try {
    const acceptedJob = await uploadAudio(file);
    const audioUuid = String(acceptedJob.audio_uuid);
    setResult(resultElement, "accepted", `Déposé — ${audioUuid}`);
    onAccepted(acceptedJob);
    return true;
  } catch (error) {
    const message = error instanceof Error ? error.message : "Échec de l’envoi.";
    setResult(resultElement, "failed", message);
    return false;
  }
}

async function runUploadPool(files, resultElements, onAccepted) {
  let nextIndex = 0;

  async function worker() {
    while (nextIndex < files.length) {
      const index = nextIndex;
      nextIndex += 1;
      await processUpload(files[index], resultElements[index], onAccepted);
    }
  }

  const workerCount = Math.min(MAX_CONCURRENT_UPLOADS, files.length);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
}

export function createUploadController({
  form,
  fileInput,
  selectedFiles,
  resultsContainer,
  results,
  feedback,
  submitButton,
  onAccepted = () => {},
}) {
  function describeSelection() {
    const count = fileInput.files?.length ?? 0;
    selectedFiles.textContent =
      count === 0
        ? "Aucun fichier sélectionné."
        : `${count} fichier${count > 1 ? "s" : ""} sélectionné${count > 1 ? "s" : ""}.`;
  }

  fileInput.addEventListener("change", describeSelection);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const files = Array.from(fileInput.files ?? []);

    if (files.length === 0) {
      feedback.textContent = "Sélectionnez au moins un fichier audio.";
      fileInput.focus();
      return;
    }

    results.replaceChildren();
    const resultElements = files.map((file) => createResult(file));
    results.append(...resultElements);
    resultsContainer.hidden = false;
    feedback.textContent = `${files.length} envoi${files.length > 1 ? "s" : ""} démarré${files.length > 1 ? "s" : ""}.`;
    submitButton.disabled = true;
    fileInput.disabled = true;

    await runUploadPool(files, resultElements, onAccepted);

    const successCount = resultElements.filter(
      (element) => element.dataset.state === "accepted",
    ).length;
    feedback.textContent = `${successCount} fichier${successCount > 1 ? "s" : ""} déposé${successCount > 1 ? "s" : ""} sur ${files.length}. Vous pouvez effectuer un nouveau dépôt.`;
    form.reset();
    fileInput.disabled = false;
    submitButton.disabled = false;
    describeSelection();
    fileInput.focus();
  });

  describeSelection();
}
