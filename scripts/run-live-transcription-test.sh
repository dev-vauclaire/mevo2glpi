#!/usr/bin/env bash

set -euo pipefail
set +x

usage() {
    cat <<'EOF'
Usage: scripts/run-live-transcription-test.sh [audio_path]

Lance le test réel de transcription avec une clé API saisie sans écho.

Variables optionnelles :
  LIVE_TRANSCRIBE_BASE_URL   URL de base (défaut : http://localhost:5000)
  LIVE_TRANSCRIBE_AUDIO_PATH Fichier audio (défaut : audio.m4a à la racine)
  LIVE_TRANSCRIBE_API_KEY    Clé préchargée ; sinon elle est demandée
EOF
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
    usage
    exit 0
fi

if (( $# > 1 )); then
    usage >&2
    exit 2
fi

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repository_root="$(cd -- "${script_directory}/.." && pwd -P)"
live_test_path="apps/worker-automation/tests/integration/test_transcription_client_live.py"

live_base_url="${LIVE_TRANSCRIBE_BASE_URL:-http://localhost:5000}"
live_audio_path="${1:-${LIVE_TRANSCRIBE_AUDIO_PATH:-${repository_root}/audio.m4a}}"

if [[ -z ${live_base_url//[[:space:]]/} ]]; then
    printf 'Erreur : LIVE_TRANSCRIBE_BASE_URL ne doit pas être vide.\n' >&2
    exit 2
fi

if [[ -L ${live_audio_path} ]]; then
    printf 'Erreur : le fichier audio ne doit pas être un lien symbolique.\n' >&2
    exit 2
fi

if [[ ! -f ${live_audio_path} ]]; then
    printf 'Erreur : fichier audio introuvable : %s\n' "${live_audio_path}" >&2
    exit 2
fi

if [[ ! -r ${live_audio_path} ]]; then
    printf 'Erreur : fichier audio illisible : %s\n' "${live_audio_path}" >&2
    exit 2
fi

audio_directory="$(cd -- "$(dirname -- "${live_audio_path}")" && pwd -P)"
live_audio_path="${audio_directory}/$(basename -- "${live_audio_path}")"

if [[ -n ${LIVE_TRANSCRIBE_API_KEY:-} ]]; then
    live_api_key="${LIVE_TRANSCRIBE_API_KEY}"
else
    if [[ ! -t 0 ]]; then
        printf '%s\n' \
            'Erreur : exécution non interactive ; définis LIVE_TRANSCRIBE_API_KEY.' \
            >&2
        exit 2
    fi

    if ! IFS= read -r -s -p 'Clé API de transcription : ' live_api_key; then
        printf '\nErreur : lecture de la clé API interrompue.\n' >&2
        exit 2
    fi
    printf '\n'
fi

if [[ -z ${live_api_key} ]]; then
    printf 'Erreur : LIVE_TRANSCRIBE_API_KEY ne doit pas être vide.\n' >&2
    exit 2
fi

if [[ ${live_api_key} =~ ^[[:space:]] || ${live_api_key} =~ [[:space:]]$ ]]; then
    printf '%s\n' \
        'Erreur : LIVE_TRANSCRIBE_API_KEY ne doit pas contenir d’espace périphérique.' \
        >&2
    exit 2
fi

if ! command -v uv >/dev/null 2>&1; then
    printf 'Erreur : la commande uv est introuvable.\n' >&2
    exit 127
fi

export LIVE_TRANSCRIBE_BASE_URL="${live_base_url}"
export LIVE_TRANSCRIBE_API_KEY="${live_api_key}"
export LIVE_TRANSCRIBE_AUDIO_PATH="${live_audio_path}"
export RUN_LIVE_TRANSCRIBE=1

cleanup() {
    unset \
        LIVE_TRANSCRIBE_BASE_URL \
        LIVE_TRANSCRIBE_API_KEY \
        LIVE_TRANSCRIBE_AUDIO_PATH \
        RUN_LIVE_TRANSCRIBE \
        live_api_key
}
trap cleanup EXIT

printf 'API de transcription : %s\n' "${LIVE_TRANSCRIBE_BASE_URL}"
printf 'Fichier audio : %s\n' "${LIVE_TRANSCRIBE_AUDIO_PATH}"

cd -- "${repository_root}"
uv run --frozen --all-packages pytest \
    "${live_test_path}" \
    -m live_transcribe \
    -vv
