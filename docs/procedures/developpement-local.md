# Procédure de développement local

## Objectif et périmètre

Cette procédure prépare le workspace Python, une base PostgreSQL de
développement, un volume audio local ainsi que les simulateurs externes
Microsoft Graph, GLPI et le service de transcription.

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
```

Renseigner au minimum :

- `DATABASE_URL`, déjà préremplie pour le PostgreSQL de développement ;
- `AUDIO_ROOT=./infra/docker/runtime/audios` ;
- les variables du ou des composants réellement exécutés.

Le fichier [.env.example](../../.env.example) décrit le contrat complet. Les
README des applications détaillent leurs variables propres.

## Préparer PostgreSQL

Depuis la racine du dépôt, démarrer le PostgreSQL 18 de développement et
attendre son healthcheck :

```bash
docker compose --file infra/docker/development/docker-compose.postgres.yml up --detach --wait
```

Le service écoute uniquement sur `127.0.0.1:5432`. Sa connexion correspond à
la valeur `DATABASE_URL` du fichier `.env.example` :

```text
postgresql+psycopg://gta_dev:gta_dev_only@127.0.0.1:5432/glpi_ticketing_dev
```

Si le port `5432` est déjà utilisé, choisir un autre port et adapter
`DATABASE_URL` dans `.env` :

```bash
GTA_DEV_POSTGRES_PORT=55432 docker compose --file infra/docker/development/docker-compose.postgres.yml up --detach --wait
```

Exécuter ensuite l'application de migration one-shot :

```bash
uv run glpi-ticketing-database-migrations
```

La commande doit terminer avec le code `0`. Ne lancer les applications qu'après
ce succès. Les tests marqués `database` créent leur propre PostgreSQL jetable et
n'utilisent pas cette base locale.

Commandes d'exploitation non destructives :

```bash
docker compose --file infra/docker/development/docker-compose.postgres.yml ps
docker compose --file infra/docker/development/docker-compose.postgres.yml stop
docker compose --file infra/docker/development/docker-compose.postgres.yml start
docker compose --file infra/docker/development/docker-compose.postgres.yml down
```

`down` supprime le conteneur et le réseau du projet, mais conserve le volume
nommé `gta-development-postgres18-data`. Ne pas utiliser `down -v`, qui
supprimerait les données locales.

PostgreSQL 18 stocke ses données dans une arborescence différente des versions
16 et 17. Le Compose monte donc le volume sur `/var/lib/postgresql`. Un ancien
volume PostgreSQL 16 ne doit pas être réutilisé directement : conserver ce
volume séparément et effectuer une migration explicite si ses données doivent
être reprises.

## Lancer les applications

Lancer uniquement les processus dont les dépendances locales sont configurées,
chacun dans un terminal séparé :

```bash
uv run glpi-ticketing-web
uv run --package glpi-ticketing-worker-publisher glpi-ticketing-publisher
uv run glpi-ticketing-worker
uv run --package glpi-ticketing-worker-retention glpi-ticketing-worker-retention
```

Vérifier le web :

```bash
curl --fail http://127.0.0.1:8000/health
```

Une seconde instance d'un même worker est refusée par son verrou advisory et
se termine avec le code `3`.

Pour exercer le flux complet sans service métier réel :

- utiliser les simulateurs décrits dans
  [`infra/docker/simulators`](../../infra/docker/simulators/README.md) ;
- utiliser la stack CPU décrite dans
  [`infra/docker/transcription`](../../infra/docker/transcription/README.md) ;
- conserver des réseaux, volumes et données distincts des environnements
  staging et production.

## Reproduire la CI

Qualité et suites locales :

```bash
uv run ruff format --check .
uv run ruff check .
uv run --all-packages pytest apps packages tests -m "not database and not deployment and not glpi and not transcription_model and not transcribe_ai and not e2e" --maxfail=1
uv run pytest providers --maxfail=1
```

Intégrations PostgreSQL jetables :

```bash
uv run --all-packages pytest -m database -vv --maxfail=1
```

Valider les manifestes sans démarrer les services :

```bash
docker compose --file infra/docker/development/docker-compose.postgres.yml config --quiet
docker compose --env-file ./infra/docker/apps/.env.example --file infra/docker/apps/docker-compose.prod.yml config --quiet
docker compose --env-file ./infra/docker/apps/staging.env.example --file infra/docker/apps/docker-compose.staging.yml config --quiet
docker compose --env-file ./infra/docker/simulators/simulators.env.example --file infra/docker/simulators/docker-compose.simulators.yml config --quiet
docker compose --env-file ./infra/docker/transcription/transcription.env.example --file infra/docker/transcription/docker-compose.transcription.yml config --quiet
```

Les fichiers d'exemple contiennent exclusivement des valeurs fictives. Ne pas
remplacer ces commandes par un fichier d'environnement réel dans les logs ou
dans une CI.

## Validation attendue

- `uv sync --locked` ne modifie pas `uv.lock` ;
- la migration atteint la révision Alembic courante ;
- le web répond sur `/health` lorsqu'il est lancé ;
- Ruff et les suites pertinentes terminent sans erreur ;
- `git status --short` ne montre aucun secret ni artefact local à versionner.

## Diagnostic et nettoyage

- une erreur de connexion PostgreSQL impose de vérifier l'hôte, le port et le
  certificat configurés dans `DATABASE_URL` ;
- un conflit singleton signifie qu'une autre instance du worker détient déjà
  son verrou ;
- une erreur TLS doit être corrigée en fournissant la CA attendue, jamais en
  désactivant la vérification ;
- les fichiers `.env`, certificats, clés et audios locaux restent hors Git.

Avant de supprimer une base, un volume ou un conteneur local, vérifier son nom
exact. Ne jamais appliquer une commande destructive à un environnement staging
ou production.
