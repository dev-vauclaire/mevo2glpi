# OpenAI-compatible transcription simulator

Simulateur stateless réservé au développement et aux tests. Il reproduit le
sous-ensemble synchrone strict consommé par `OpenAICompatibleTranscriber` :

```text
GET  /health
POST /v1/audio/transcriptions
```

Le POST exige une authentification Bearer et les champs multipart `file`,
`model`, `language` et `response_format=json`. Il renvoie une transcription
fixe et synthétique sans exécuter de modèle, sans conserver l'audio et sans
journaliser le corps de la requête ou la réponse.

Il ne doit recevoir aucun audio réel.

```text
OPENAI_TRANSCRIPTION_SIMULATOR_HOST=0.0.0.0
OPENAI_TRANSCRIPTION_SIMULATOR_PORT=8080
OPENAI_TRANSCRIPTION_SIMULATOR_API_KEY=<clé synthétique locale>
OPENAI_TRANSCRIPTION_SIMULATOR_MAX_UPLOAD_BYTES=26214400
```
