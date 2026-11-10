# Procédure de développement local

## Objectif et périmètre

Cette procédure lance les applications Python depuis le workspace uv et leurs
dépendances locales dans Docker : PostgreSQL 18, GLPI 11.0.7, Microsoft Graph,
Transcribe AI et une API de transcription OpenAI-compatible.

Tous les comptes, messages et contenus utilisés localement doivent être
fictifs. Aucun secret, certificat interne, audio réel ou donnée sensible ne doit
être copié dans cet environnement.

Pour découvrir Keycloak indépendamment de l'application avant toute intégration
OIDC, suivre le [laboratoire Keycloak local](keycloak-local.md). Il utilise son
propre Compose et sa propre base PostgreSQL ; il n'est pas nécessaire au
fonctionnement local actuel de mevo2glpi.

## Prérequis

- Git ;
- Python 3.12 ou supérieur ;
- uv ;
- Docker Engine et Docker Compose.

Le dépôt utilise un unique `uv.lock`. Il ne faut pas installer séparément les
dépendances de chaque application.

## Préparer le workspace

Depuis la racine du dépôt :

```bash
uv sync --locked --all-packages --all-groups
cp .env.example .env
mkdir -p infra/docker/runtime/audios
mkdir -p infra/docker/runtime/secrets
umask 077
openssl rand -base64 32 > infra/docker/runtime/secrets/originating-phone-number.key
```

Le fichier `.env.example` est prérempli avec des identifiants exclusivement
synthétiques et des URLs loopback. Le fichier `.env` reste local et ne doit
jamais être commité.

La clé locale protège temporairement le numéro d'origine extrait des objets
Graph. Elle est synthétique, commune aux deux workers et reste dans le
répertoire `runtime` ignoré par Git.

## Démarrer PostgreSQL

```bash
docker compose --file infra/docker/development/docker-compose.postgres.yml up --detach --wait
```

PostgreSQL écoute uniquement sur `127.0.0.1:5432`. Si ce port est déjà utilisé,
adapter ensemble `MEVO2GLPI_DEV_POSTGRES_PORT` dans l'environnement de la
commande et le port de `DATABASE_URL` dans `.env`.

PostgreSQL 18 monte son volume sur `/var/lib/postgresql`. Le volume nommé
`mevo2glpi-development-postgres18-data` ne doit pas être remplacé par un
ancien volume PostgreSQL 16 ou 17 sans migration explicite.

Exécuter ensuite la migration one-shot :

```bash
uv run --package mevo2glpi-database-migrations mevo2glpi-database-migrations
```

La commande doit terminer avec le code `0` avant le lancement des applications.

## Démarrer les services externes de test

Construire puis attendre les quatre services HTTP. MySQL est démarré
automatiquement comme dépendance privée de GLPI :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml up --detach --build --wait microsoft-graph-simulator transcribe-ai-simulator openai-transcription-simulator glpi-test
```

Initialiser ensuite l'API V2, le client OAuth Password et la source GLPI
`MEVO`. Le bootstrap est idempotent :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml run --rm glpi-bootstrap
```

Les services sont accessibles uniquement depuis la machine locale :

| Service | URL locale | Usage |
| --- | --- | --- |
| Microsoft Graph simulé | `http://127.0.0.1:18080` | boîte Exchange synthétique |
| Transcribe AI simulé | `http://127.0.0.1:18081` | contrat historique avec polling |
| Transcription OpenAI simulée | `http://127.0.0.1:18082/v1` | contrat synchrone sans modèle |
| GLPI 11.0.7 | `http://127.0.0.1:18083` | API et interface de test |

MySQL GLPI n'est jamais publié sur l'hôte. Les quatre ports HTTP peuvent être
modifiés avec les variables `MEVO2GLPI_DEV_*_PORT` documentées dans
`.env.example` ;
les URLs applicatives de `.env` doivent alors être adaptées.

Vérifier les services :

```bash
curl --fail http://127.0.0.1:18080/health
curl --fail http://127.0.0.1:18081/health
curl --fail http://127.0.0.1:18082/health
curl --fail http://127.0.0.1:18083/api.php/v2.3.0/doc.json
```

Récupérer l'identifiant local de la source `MEVO` :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml exec -T glpi-test php /opt/glpi-test/bootstrap_request_source.php
```

Reporter la valeur JSON `id` dans `GLPI_TICKET_REQUEST_SOURCE_ID` du fichier
`.env` avant de lancer le worker d'automatisation.

Injecter un message audio synthétique dans la boîte Graph :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml exec microsoft-graph-simulator mevo2glpi-microsoft-graph-simulator-admin seed --scenario valid-wav
```

La liste des scénarios est disponible avec :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml exec microsoft-graph-simulator mevo2glpi-microsoft-graph-simulator-admin list-scenarios
```

## Choisir le provider de transcription

Les deux simulateurs peuvent rester démarrés, mais le worker n'en utilise qu'un
à la fois.

Le contrat OpenAI-compatible synchrone est sélectionné par défaut :

```dotenv
TRANSCRIPTION_PROVIDER=openai_compatible
OPENAI_COMPATIBLE_TRANSCRIPTION_BASE_URL=http://127.0.0.1:18082/v1
```

Pour tester l'ancien contrat avec polling :

```dotenv
TRANSCRIPTION_PROVIDER=legacy_polling
LEGACY_TRANSCRIPTION_BASE_URL=http://127.0.0.1:18081
```

Le simulateur OpenAI-compatible retourne un texte fixe et ne mesure pas la
qualité d'une transcription. Pour utiliser le véritable moteur Parakeet, suivre
la documentation de [`infra/docker/transcription`](../../infra/docker/transcription/README.md)
et adapter l'URL ainsi que la clé dans `.env`.

## Lancer les applications

Lancer chaque processus dans un terminal séparé :

Dans ce workspace, `--package` sélectionne explicitement le membre uv dont les
dépendances et le script doivent être utilisés. Le dernier argument est le
script console déclaré par ce membre ; son nom peut donc différer de celui du
package.

```bash
uv run --package mevo2glpi-web mevo2glpi-web
uv run --package mevo2glpi-worker-publisher mevo2glpi-worker-publisher
uv run --package mevo2glpi-worker-automation mevo2glpi-worker-automation
uv run --package mevo2glpi-worker-retention mevo2glpi-worker-retention
```

Vérifier le web :

```bash
curl --fail http://127.0.0.1:8000/health
```

Une seconde instance d'un même worker est refusée par son verrou PostgreSQL et
se termine avec le code `3`.

## Arrêter ou réinitialiser l'environnement

Arrêt non destructif :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml down
docker compose --file infra/docker/development/docker-compose.postgres.yml down
```

Ces commandes conservent les volumes nommés. Pour repartir d'un GLPI et d'un
MySQL neufs, la commande suivante supprime uniquement les volumes du projet
local des services de test :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml down --volumes
```

Cette suppression est irréversible. Ne jamais réutiliser cette commande contre
un projet staging ou production.

## Reproduire les validations

```bash
uv run ruff format --check .
uv run ruff check .
uv run --all-packages pytest apps packages tests -m "not database and not deployment and not glpi and not transcription_model and not transcribe_ai and not e2e" --maxfail=1
uv run pytest providers --maxfail=1
uv run --all-packages pytest -m database -vv --maxfail=1
```

Valider les manifestes sans démarrer les services :

```bash
docker compose --file infra/docker/development/docker-compose.postgres.yml config --quiet
docker compose --env-file infra/docker/development/keycloak.env.example --file infra/docker/development/docker-compose.keycloak.yml config --quiet
docker compose --env-file .env.example --file infra/docker/development/docker-compose.test-services.yml config --quiet
docker compose --env-file ./infra/docker/apps/.env.example --file infra/docker/apps/docker-compose.prod.yml config --quiet
docker compose --env-file ./infra/docker/apps/staging.env.example --file infra/docker/apps/docker-compose.staging.yml config --quiet
docker compose --env-file ./infra/docker/providers/staging.env.example --file infra/docker/providers/docker-compose.staging.yml config --quiet
docker compose --env-file ./infra/docker/transcription/staging.env.example --file infra/docker/transcription/docker-compose.transcription.yml config --quiet
```

Les tests marqués `database` et `glpi` utilisent leurs propres environnements
jetables et ne manipulent pas les volumes locaux décrits ici.

## Diagnostic

- vérifier les conteneurs avec `docker compose ... ps` avant de lire les logs ;
- une erreur de connexion PostgreSQL impose de vérifier ensemble le port publié
  et `DATABASE_URL` ;
- une erreur GLPI après modification des identifiants locaux nécessite un
  nouveau volume ou une rotation explicite, jamais une réconciliation forcée ;
- une erreur TLS doit être corrigée avec la CA attendue, jamais en désactivant
  la vérification ;
- les fichiers `.env`, certificats, clés et audios locaux restent hors Git.
