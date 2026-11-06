# Transcribe AI simulator

Simulateur stateful réservé au développement, au staging et aux tests. Il
reproduit la soumission multipart et le polling de Transcribe AI : un job est
`PENDING` au premier polling puis `COMPLETED` avec une transcription fixe et
synthétique.

Il ne doit recevoir aucun audio réel.

```text
TRANSCRIBE_AI_SIMULATOR_HOST=0.0.0.0
TRANSCRIBE_AI_SIMULATOR_PORT=8080
TRANSCRIBE_AI_SIMULATOR_API_KEY=<clé synthétique>
```

Depuis le conteneur, la recette opérateur ne journalise pas la transcription :

```bash
mevo2glpi-transcribe-ai-simulator-smoke
```
