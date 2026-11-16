# Étape 4 — Démarrer Microsoft Graph simulé

## Objectif

Fournir au publisher une boîte Exchange synthétique sans contacter Microsoft
365. Cette étape est obligatoire uniquement lorsque le publisher est lancé.

## Démarrer le simulateur

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml up --detach --build --wait microsoft-graph-simulator
```

Le service est publié uniquement sur :

```text
http://127.0.0.1:18080
```

La configuration applicative doit contenir :

```dotenv
PUBLISHER_GRAPH_AUTH_MODE=synthetic
PUBLISHER_GRAPH_BASE_URL=http://127.0.0.1:18080/v1.0
```

`PUBLISHER_MAILBOX_ID` reste un UUID fictif et doit correspondre à la boîte du
simulateur. Les variables Entra et les fichiers PEM ne sont pas utilisés dans
ce mode.

## Vérifier le service

```bash
curl --fail http://127.0.0.1:18080/health
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml ps microsoft-graph-simulator
```

## Manipuler les scénarios

Lister le catalogue :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml exec microsoft-graph-simulator mevo2glpi-microsoft-graph-simulator-admin list-scenarios
```

Créer un message WAV synthétique valide :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml exec microsoft-graph-simulator mevo2glpi-microsoft-graph-simulator-admin seed --scenario valid-wav
```

Pour une validation manuelle étendue, la commande `seed-suite` injecte le
catalogue complet et affiche chaque cas créé. Aucun fichier provenant d'une
boîte réelle ne doit être utilisé.

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml exec microsoft-graph-simulator mevo2glpi-microsoft-graph-simulator-admin seed-suite
```

## Résultat attendu

Le publisher découvre les messages via le contrat Graph simulé et peut les
classer dans les dossiers synthétiques `Processed` ou `Rejected` sans accès
réseau à Microsoft 365.

## Diagnostic

- vérifier le port `MEVO2GLPI_DEV_MICROSOFT_GRAPH_PORT` si la sonde échoue ;
- vérifier que `PUBLISHER_GRAPH_BASE_URL` se termine par `/v1.0` ;
- consulter les logs avec `docker compose ... logs microsoft-graph-simulator` ;
- ne jamais remplacer le mode `synthetic` par des identifiants Entra réels en
  développement local.

## Étape suivante

Préparer [GLPI](05-glpi-optionnel.md) si automation ou retention sont lancés.
