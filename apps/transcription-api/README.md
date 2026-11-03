# Transcription API

Service interne de transcription CPU. Il expose le sous-ensemble synchrone du
contrat OpenAI consommé par `worker-automation` et conserve toute la préparation
audio dans le conteneur de transcription.

## Flux

```text
multipart WAV/OGG/MP3/M4A
        -> validation et limite d'upload
        -> FFmpeg : float32 mono 16 kHz
        -> Parakeet Ultra via transcribe.cpp (CPU)
        -> {"text": "..."}
```

Routes :

- `POST /v1/audio/transcriptions` ;
- `GET /health/live` ;
- `GET /health/ready`.

Le modèle Q8 est fourni et vérifié par l'image Docker. Il n'est jamais téléchargé
au démarrage du service. Le binding `transcribe.cpp` et son runtime natif doivent
rester à la même version pré-1.0.

Le champ OpenAI `language=fr` est transmis au moteur, mais Parakeet Ultra repose
sur son auto-détection et ne garantit pas le forçage de la langue. La validation
du français fait donc partie des critères obligatoires de promotion.

Le service ne journalise ni le fichier audio, ni son nom, ni la transcription.

## Configuration

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `TRANSCRIPTION_API_KEY` | obligatoire | Jeton Bearer interne |
| `TRANSCRIPTION_MODEL_PATH` | `/models/parakeet-ultra-Q8_0.gguf` | GGUF monté en lecture seule |
| `TRANSCRIPTION_MODEL_NAME` | `parakeet-ultra` | Modèle accepté par le contrat HTTP |
| `TRANSCRIPTION_CPU_THREADS` | `2` | Threads d'inférence CPU |
| `TRANSCRIPTION_HOST` | `127.0.0.1` | Adresse d'écoute |
| `TRANSCRIPTION_PORT` | `5092` | Port HTTP |
| `TRANSCRIPTION_LOG_LEVEL` | `info` | Niveau de journalisation |
| `TRANSCRIPTION_MAX_UPLOAD_BYTES` | `26214400` | Taille maximale d'un upload |
| `TRANSCRIPTION_MAX_AUDIO_SECONDS` | `600` | Durée maximale après décodage |
| `TRANSCRIPTION_FFMPEG_PATH` | `/usr/bin/ffmpeg` | Exécutable de normalisation |
| `TRANSCRIPTION_FFMPEG_TIMEOUT_SECONDS` | `60` | Timeout de normalisation |

Le service n'est pas destiné à être publié sur Internet. Il doit rester sur le
réseau Docker privé partagé avec `worker-automation`.

## Validation du modèle réel

Les tests ordinaires utilisent un moteur injecté et ne téléchargent jamais le
GGUF. Le smoke test CPU réel est volontairement séparé :

```bash
TRANSCRIPTION_MODEL_TEST_PATH=/models/parakeet-ultra-Q8_0.gguf \
  uv run --package glpi-ticketing-transcription-api \
  pytest -m transcription_model \
  apps/transcription-api/tests/integration/test_real_model.py -vv
```

Avant une promotion en production, ce test doit être complété en staging par
des messages vocaux fictifs représentatifs. Une vigilance particulière reste
nécessaire sur l'auto-détection du français et sur les audios de plus de
30 secondes présentant de fortes variations de volume.
