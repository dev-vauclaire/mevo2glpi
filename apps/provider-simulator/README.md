# Provider simulator

`provider-simulator` est une application strictement réservée au staging. Elle
remplace les deux dépendances qui ne disposent pas d'une instance de recette :

- Microsoft Graph et la boîte Exchange dédiée ;
- Transcribe AI et son worker GPU.

Elle ne doit jamais être déployée en production ni recevoir un audio, un
message ou une transcription réels.

## Contrats simulés

Le même processus HTTP expose :

- un sous-ensemble stateful de Microsoft Graph v1.0 ;
- `POST /api/batchTranscription/createJob` ;
- `GET /api/batchTranscription/result` ;
- `GET /health` ;
- deux routes d'administration internes pour injecter ou effacer des données
  synthétiques.

Les messages conservent leur identifiant immuable lorsqu'ils sont déplacés
entre `Inbox`, `Processed` et `Rejected`. Une transcription reste `PENDING`
pendant le premier polling, puis devient `COMPLETED` avec un texte synthétique
fixe. L'état est uniquement conservé en mémoire et disparaît au redémarrage.

## Configuration

```text
PROVIDER_SIMULATOR_HOST=0.0.0.0
PROVIDER_SIMULATOR_PORT=8080
PROVIDER_SIMULATOR_MAILBOX_ID=<UUID fictif>
PROVIDER_SIMULATOR_ADMIN_TOKEN=<secret staging>
PROVIDER_SIMULATOR_TRANSCRIPTION_API_KEY=<clé synthétique staging>
```

Le service n'est ni publié sur l'hôte ni relié à Traefik. Le jeton
d'administration n'est transmis à aucun worker.

## Recette opérateur

Depuis le terminal Dokploy du conteneur :

```bash
glpi-ticketing-provider-simulator-admin seed --extension wav
glpi-ticketing-provider-simulator-admin seed --extension ogg --received-at 2026-10-27T08:00:00Z
glpi-ticketing-provider-simulator-admin reset
```

Les fichiers WAV et OGG générés sont de petits conteneurs audio sans voix ni
donnée personnelle. La commande n'affiche jamais le jeton d'administration.

## Validation locale

```bash
uv run pytest apps/provider-simulator/tests
uv run ruff check apps/provider-simulator
```
