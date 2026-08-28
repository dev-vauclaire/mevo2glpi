"use strict";

const audioInput = document.querySelector("[data-audio-input]");
const selectedFile = document.querySelector("[data-selected-file]");

if (audioInput instanceof HTMLInputElement && selectedFile !== null) {
  audioInput.addEventListener("change", () => {
    const filename = audioInput.files?.item(0)?.name;
    selectedFile.textContent = filename ? `Fichier sélectionné : ${filename}` : "";
  });
}
