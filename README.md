# GLPI Ticketing Automation

GLPI Ticketing Automation est un MVP qui transforme les messages vocaux d'une
boîte Exchange Online dédiée en tickets GLPI. Le worker publisher ingère leur
pièce jointe WAV ou OGG, le worker d'automatisation demande la transcription à
Transcribe AI puis crée le ticket GLPI, et le worker de rétention applique la
politique de conservation. L'application web est une interface de consultation
et d'action opérateur ; elle ne dépose plus d'audio.

PostgreSQL est l'unique source de vérité du cycle de vie. Les octets audio sont
conservés dans un volume partagé sous une clé canonique immuable. Le projet est
prévu pour un déploiement mono-serveur avec Docker Compose et Dokploy.

La branche stable est `main`. Les versions du MVP sont archivées par les tags
`mvp-vX.Y.Z` ; la dernière baseline publiée est `mvp-v0.1.1`. L'ancien POC à
quatre répertoires reste consultable sous le
tag `poc-v0.1.0`, mais il ne décrit plus le code courant. Le passage du POC au
MVP suppose une base PostgreSQL et un volume audio neufs ; aucune migration des
anciens répertoires n'est fournie.

## Fonctionnalités du MVP

- ingestion dédiée des messages vocaux Exchange Online par le worker publisher,
  avec identifiants Graph immuables, contrôle WAV/OGG et acquittement dans les
  dossiers `Processed` ou `Rejected` ;
- suivi paginé des jobs, consultation de leur historique expurgé et relance
  opérateur d'un job `failed` ou `blocked` ;
- persistance PostgreSQL des transitions, tentatives, identifiants distants,
  rétentions et heartbeats ;
- reprise autonome après redémarrage, retry avec backoff exponentiel et
  distinction entre indisponibilité, échec terminal et blocage ;
- transcription par POST puis polling auprès de Transcribe AI, sans persister
  le texte de la transcription ;
- création de tickets avec l'API GLPI V2.3.0 et corrélation
  `external_id = audio_uuid` ;
- conservation des audios terminés jusqu'à la fermeture du ticket GLPI ou
  jusqu'à leur deadline absolue ;
- dashboard interne présentant l'état des workers et les alertes
  opérationnelles ;
- déploiement Dokploy composé de la migration one-shot, du web et des trois
  workers.

## Architecture d'exécution

```mermaid
flowchart LR
    U[Opérateur] --> T[Traefik Dokploy]
    T --> W[Application web]

    B[Boîte Exchange Online] --> WP[Worker publisher]
    WP -->|client_credentials| E[Entra ID]
    WP -->|Graph v1.0| B

    M[Migration Alembic one-shot] --> DB[(PostgreSQL externe)]
    W --> DB
    W -->|lecture| A[(Volume audio)]
    WP --> DB
    WP -->|publication| A

    WA[Worker automation] --> DB
    WA -->|lecture| A
    WA --> TA[Transcribe AI]
    WA --> G[GLPI V2.3.0]

    WR[Worker retention] --> DB
    WR -->|purge| A
    WR --> G
```

Au démarrage du Compose, `database-migrations` attend PostgreSQL, exécute
`alembic upgrade head`, puis s'arrête. Le web et les trois workers démarrent
après le succès de cette migration ; aucun ordre supplémentaire n'est requis
entre ces quatre services persistants.

PostgreSQL, GLPI, Transcribe AI et Traefik ne sont pas embarqués dans le
Compose de production :

- PostgreSQL est provisionné séparément et rejoint par un réseau Docker privé ;
- GLPI, Transcribe AI, Entra ID et Graph sont accessibles par les workers ;
- Traefik est fourni et configuré par Dokploy ;
- le volume nommé des audios est le seul stockage persistant créé par le
  Compose.

### Workspace uv

La racine est un [workspace uv](pyproject.toml) non publiable
(`package = false`). Elle centralise `uv.lock`, les dépendances de
développement et la configuration de Pytest et Ruff.

| Membre | Distribution Python | Responsabilité | Documentation |
| --- | --- | --- | --- |
| `apps/database-migrations` | `glpi-ticketing-database-migrations` | Migration Alembic one-shot et historique du schéma | [Migrations](apps/database-migrations/README.md) |
| `apps/web` | `glpi-ticketing-web` | API de consultation, dashboard, retry opérateur et téléchargement | [Application web](apps/web/README.md) |
| `apps/worker-publisher` | `glpi-ticketing-worker-publisher` | Ingestion Exchange Online, validation WAV/OGG, publication atomique et acquittement Graph | [Worker publisher](apps/worker-publisher/README.md) |
| `apps/worker-automation` | `glpi-ticketing-worker-automation` | Transcription, création GLPI et finalisation des jobs | [Worker d'automatisation](apps/worker-automation/README.md) |
| `apps/worker-retention` | `glpi-ticketing-worker-retention` | Contrôle GLPI, deadlines et purge crash-safe | [Worker de rétention](apps/worker-retention/README.md) |
| `packages/common` | `glpi-ticketing-common` | Stockage audio canonique, attente coopérative, backoff et healthchecks | [Package commun](packages/common/README.md) |
| `packages/database` | `glpi-ticketing-database` | Modèles SQLAlchemy, sessions, repositories et verrous advisory | [Package database](packages/database/README.md) |
| `packages/glpi-client` | `glpi-ticketing-glpi` | Client OAuth Password et contrats GLPI V2.3.0 | [Client GLPI](packages/glpi-client/README.md) |

Chaque membre utilise un layout `src`, possède son propre `pyproject.toml`
et garde ses tests dans son répertoire `tests`. Une application ne doit pas
importer les modules internes d'une autre application : le code partagé vit
dans `packages`.

## Cycle de vie d'un job

```mermaid
flowchart TD
    Exchange[Message Exchange + WAV/OGG] --> Publish[Worker publisher]
    Publish --> Audio[Publier l'audio canonique]
    Audio --> Pending[(pending + job_created)]
    Pending --> Select[Sélection PostgreSQL]
    RetryDue[(retry_scheduled dû)] --> Select
    Resume[(processing abandonné)] --> Select

    Select --> Known{glpi_ticket_id connu ?}
    Known -->|oui| Finalize[Finalisation locale]
    Known -->|non| Transcribe[POST et polling Transcribe AI]
    Transcribe --> Ticket[Recherche ou création GLPI]
    Ticket --> Persist[Persister glpi_ticket_id]
    Persist --> Finalize
    Finalize --> Completed[(completed + rétention)]

    Transcribe -->|indisponibilité| Retry[(retry_scheduled + backoff)]
    Ticket -->|indisponibilité| Retry
    Retry --> RetryDue

    Transcribe -->|rejet terminal du job| Failed[(failed)]
    Ticket -->|rejet terminal du job| Failed
    Transcribe -->|ambiguïté du job| Blocked[(blocked)]
    Ticket -->|ambiguïté du job| Blocked
```

### Ingestion Exchange

Le web n'accepte plus de création de job. Le worker publisher demande un jeton
applicatif Entra ID, pagine l'Inbox avec Microsoft Graph et persiste les
messages en utilisant `Prefer: IdType="ImmutableId"`. Il reprend toujours un
traitement durable avant de découvrir de nouveaux messages.

Un message doit contenir exactement une pièce jointe WAV ou OGG non vide,
structurellement valide et d'au plus 25 Mio. Le worker la télécharge dans un
fichier temporaire, publie la clé canonique `{ingestion_uuid}.{extension}`, puis
crée le job `pending` et son événement dans une transaction. Le message est
déplacé dans `Processed`
uniquement après cette frontière durable. Un message invalide est enregistré
avec un motif expurgé avant son déplacement dans `Rejected`.

Les appels temporaires en échec sont rejoués avec backoff. Une réponse perdue
pendant un déplacement Graph est réconciliée en relisant le dossier courant du
message. Les identifiants opaques Exchange, jetons, noms de pièces jointes et
contenus audio ne sont jamais journalisés.

### Worker d'automatisation

Le worker est singleton grâce à un verrou advisory PostgreSQL. À chaque
sélection, il traite dans cet ordre :

1. le job déjà `processing`, pour reprendre un traitement interrompu ;
2. le retry arrivé à échéance ;
3. le plus ancien job `pending`.

Il résout directement `AUDIO_ROOT / storage_key` sans scanner le volume, puis
vérifie le type du fichier et sa taille. Si `glpi_ticket_id` est déjà
persisté, cela constitue la confirmation durable de la création du ticket : le
worker finalise localement sans rappeler Transcribe AI ni GLPI.

Dans le cas contraire, le worker soumet l'audio à Transcribe AI, persiste
l'identifiant du job distant, récupère la transcription par polling puis
appelle GLPI. Le texte transcrit ne vit qu'en mémoire et n'apparaît ni dans la
base, ni dans les événements, ni dans les logs. L'identifiant GLPI est persisté
avant la transition finale `completed`.

Transcribe AI ne persistant pas encore ses résultats, un redémarrage ou un
retry sans `glpi_ticket_id` provoque une nouvelle transcription. Le client
GLPI utilise `external_id = audio_uuid` pour retrouver ou créer le ticket et
limiter les doublons lors d'une issue distante incertaine.

### États et erreurs

| État | Signification |
| --- | --- |
| `pending` | Le dépôt est durable et attend sa première prise en charge. |
| `processing` | Le worker possède le job ou reprend une tentative interrompue. |
| `retry_scheduled` | Une indisponibilité temporaire a une prochaine échéance persistée. |
| `completed` | Le ticket GLPI est confirmé et sa rétention est planifiée. |
| `failed` | Une erreur terminale propre aux données ou au job est confirmée. |
| `blocked` | Une ambiguïté propre au job exige une décision opérateur. |

Les étapes persistées sont `transcription`, `glpi_creation` et
`finalization`. Elles indiquent la dernière frontière atteinte, tandis que
l'état indique ce que l'orchestrateur peut faire ensuite.

Une erreur réseau ou un statut temporaire produit `retry_scheduled`. Le
backoff exponentiel vaut par défaut 5, 10, 20, 40, 80, 160 puis 300 secondes.
Le délai reste ensuite plafonné à 300 secondes.

Un job devient `failed` uniquement lorsqu'une nouvelle tentative avec les
mêmes données n'a pas de chance raisonnable de réussir : rejet métier explicite
de Transcribe AI, statut distant `FAILED`, transcription invalide ou rejet
sémantique GLPI reconnu.

Un job devient `blocked` lorsqu'une incohérence ou une ambiguïté limitée à ce
job rendrait une relance automatique dangereuse : fichier absent ou incohérent,
plusieurs tickets pour le même `external_id`, par exemple. Le dashboard peut
réarmer un job `failed` ou `blocked` si son audio est toujours valide.

Un blocage système est distinct : le job peut rester `processing`, le
heartbeat du worker devient `blocked` et la consommation de la file est
suspendue. Le worker sonde automatiquement le service seulement lorsque cette
sonde est sûre ; une ambiguïté verrouillée nécessite un redémarrage contrôlé ou
une décision opérateur.

### Rétention

Le worker de rétention est lui aussi singleton. Pour un job `completed`, il
relit GLPI par le `glpi_ticket_id` persisté et vérifie que
`external_id == audio_uuid`. Seul un ticket `CLOSED` autorise la suppression
avant la deadline ; un ticket `SOLVED` reste conservé.

Les valeurs par défaut sont :

- `completed` : suppression à la fermeture du ticket ou au plus tard après
  90 jours ;
- `failed` : suppression après 30 jours ;
- `blocked` : suppression après 90 jours.

La deadline absolue s'applique même si GLPI est indisponible. La suppression
utilise une quarantaine privée `.purge` et des transitions PostgreSQL
rejouables afin de reprendre après un crash. La ligne du job et son historique
restent en base après la suppression des octets ; l'API de téléchargement
retourne alors `410 Gone`.

## Interface web et API

L'interface `GET /` propose des filtres par état et UUID, la pagination, un
détail avec événements, le téléchargement, la relance opérateur et les alertes.
Elle actualise les jobs et l'observabilité toutes les dix secondes lorsque
l'onglet est visible.

| Route | Rôle |
| --- | --- |
| `GET /health` | Liveness du processus web, sans sonder PostgreSQL |
| `GET /` | Dashboard interne |
| `GET /api/jobs` | Pagination par curseur, filtre d'état et recherche UUID exacte |
| `GET /api/jobs/{audio_uuid}` | Détail persistant et état de rétention |
| `GET /api/jobs/{audio_uuid}/events` | Historique expurgé, paginé séparément |
| `POST /api/jobs/{audio_uuid}/retry` | Réarme un job `failed` ou `blocked` et retourne `202` |
| `GET /api/observability` | Heartbeats et alertes opérationnelles agrégées |
| `GET /audios/{audio_uuid}` | Télécharge un audio terminé non purgé |

La pagination est fondée sur un curseur opaque `(updated_at, uuid)`. Les
alertes couvrent notamment les workers absents, arrêtés, périmés, bloqués ou
dégradés, les jobs en retard, `failed` ou `blocked`, et les rétentions
bloquées ou échues. Elles représentent un instantané : il n'existe pas encore
d'acquittement, d'historique d'alerte ni de notification externe.

Le MVP n'intègre pas d'authentification HTTP. Le dashboard et l'API doivent être
limités au réseau interne par le DNS, le pare-feu ou un middleware Traefik.

## Technologies

Les contraintes des `pyproject.toml` sont la source de vérité et `uv.lock`
épingle l'environnement résolu.

| Technologie | Utilisation |
| --- | --- |
| Python 3.12+ | Langage des huit membres du workspace |
| uv | Workspace, verrouillage, exécution et build |
| FastAPI, Uvicorn, Jinja2 | API HTTP, serveur ASGI et dashboard |
| Pydantic Settings | Configuration typée par variables d'environnement |
| SQLAlchemy 2, Psycopg 3 | Modèles, repositories et accès PostgreSQL synchrone |
| Alembic | Migrations one-shot du schéma |
| HTTPX, HTTPX2 | Clients Transcribe AI, GLPI et Microsoft Graph |
| MSAL | Authentification applicative Entra ID du publisher par clé privée et certificat X.509 |
| Pytest, Testcontainers | Tests unitaires et stacks d'intégration jetables |
| Ruff | Formatage et analyse statique |
| Docker Compose, Dokploy, Traefik | Packaging et déploiement mono-serveur |

## Prérequis

Pour développer ou lancer les applications localement :

- Python 3.12 ou supérieur ;
- [uv](https://docs.astral.sh/uv/) ;
- une base PostgreSQL joignable ;
- une racine audio existante et inscriptible ;
- une instance Transcribe AI et une instance GLPI pour exécuter le pipeline
  complet ;
- une application Entra ID autorisée par administrateur, limitée à la boîte
  Exchange dédiée, ainsi que sa clé privée PEM et son certificat public X.509
  PEM dans deux fichiers distincts pour lancer le publisher réel.

Pour les suites marquées `database`, `deployment`, `glpi`,
`transcribe_ai` ou `e2e`, Docker Engine et le plugin Compose sont requis.
Les tests Transcribe AI nécessitent un hôte et un daemon `linux/amd64`, un
accès réseau pour télécharger le modèle Whisper `tiny` et suffisamment
d'espace disque pour l'image STT. Le mode GPU optionnel exige un GPU NVIDIA et
NVIDIA Container Toolkit.

Pour Dokploy, le serveur doit fournir Dokploy `v0.30.10`, version validée pour
ce projet, ainsi qu'une version récente de Docker Compose, Traefik, un
PostgreSQL externe et les deux réseaux Docker décrits plus bas.

## Installation et démarrage local

Depuis la racine du dépôt :

```bash
uv sync --all-packages --all-groups
cp .env.example .env
mkdir -p infra/docker/runtime/audios
```

Renseignez les secrets et les URL dans `.env`, puis utilisez une racine locale
inscriptible :

```dotenv
AUDIO_ROOT=./infra/docker/runtime/audios
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/glpi_ticketing
```

Le mot de passe de `DATABASE_URL` doit être encodé comme composant d'URL. Les
settings lisent `.env` relativement au répertoire courant : lancez les
commandes suivantes depuis la racine.

Appliquez d'abord le schéma :

```bash
uv run glpi-ticketing-database-migrations
```

Lancez ensuite les quatre processus persistants dans des terminaux séparés :

```bash
uv run glpi-ticketing-web
```

```bash
uv run --package glpi-ticketing-worker-publisher \
  glpi-ticketing-publisher
```

```bash
uv run glpi-ticketing-worker
```

```bash
uv run --package glpi-ticketing-worker-retention \
  glpi-ticketing-worker-retention
```

Vérifiez le web :

```bash
curl --fail http://127.0.0.1:8000/health
```

Les formes `python -m glpi_ticketing_web`,
`python -m glpi_ticketing_worker_publisher`,
`python -m glpi_ticketing_worker_automation` et
`python -m glpi_ticketing_worker_retention` sont également prises en charge
dans leur environnement uv.

Une seconde instance de l'un des workers est refusée par son verrou singleton
et termine avec le code `3`.

## Configuration et secrets

Deux modèles sont fournis :

- [.env.example](.env.example) pour lancer les processus depuis la racine ;
- [infra/docker/apps/.env.example](infra/docker/apps/.env.example) pour Dokploy, avec les
  variables commentées et regroupées par service.

Les principales variables sont :

| Groupe | Variables structurantes |
| --- | --- |
| PostgreSQL | `DATABASE_URL`, `DATABASE_CONNECT_TIMEOUT_SECONDS` |
| Stockage et web | `AUDIO_ROOT`, `PUBLIC_BASE_URL`, `WEB_HOST`, `WEB_PORT` |
| Publisher | `PUBLISHER_ENTRA_*`, `PUBLISHER_MAILBOX_ID`, `PUBLISHER_GRAPH_*`, `PUBLISHER_*_INTERVAL_SECONDS` |
| Transcribe AI | `LEGACY_TRANSCRIPTION_BASE_URL`, `LEGACY_TRANSCRIPTION_API_KEY`, timeouts et polling `TRANSCRIPTION_*` |
| GLPI | `GLPI_BASE_URL`, `GLPI_USERNAME`, `GLPI_PASSWORD`, `GLPI_CLIENT_ID`, `GLPI_CLIENT_SECRET`, valeurs `GLPI_TICKET_*` |
| Worker | `WORKER_LOG_LEVEL`, intervalles `WORKER_*` et durées `*_RETENTION_DAYS` |
| Rétention | `RETENTION_POLL_INTERVAL_SECONDS`, `RETENTION_CHECK_INTERVAL_HOURS`, backoff `RETENTION_*` |
| Observabilité | `WORKER_HEARTBEAT_INTERVAL_SECONDS`, seuils `DASHBOARD_*` |

`WORKER_LOG_LEVEL=INFO` convient à l'exploitation courante.
`WORKER_LOG_LEVEL=DEBUG` ajoute temporairement des diagnostics réseau et TLS
expurgés pour GLPI ; il ne journalise ni secret, ni transcription, ni réponse
HTTP brute.

Les fichiers `.env` réels sont ignorés par Git et ne doivent jamais être
commités. Les secrets de production sont fournis par l'environnement Dokploy ;
la clé privée et le certificat du publisher restent des fichiers hôte montés en
lecture seule.

## Déploiement Dokploy

Le seul manifeste à donner à Dokploy est
[infra/docker/apps/docker-compose.prod.yml](infra/docker/apps/docker-compose.prod.yml).
Il déclare les cinq services directement afin que Dokploy puisse les découvrir,
puis réutilise leur configuration commune avec `extends` :

- `database-migrations` ;
- `web` ;
- `worker-publisher` ;
- `worker-automation` ;
- `worker-retention`.

Dokploy `v0.30.10` transmet explicitement à Compose le fichier `.env` généré à
côté du manifeste. Le correctif correspondant est disponible depuis la version
`v0.30.4` et évite la régression
[Dokploy #5252](https://github.com/Dokploy/dokploy/issues/5252). Les manifestes
n'utilisent pas de collision `include` et restent compatibles avec la
sémantique standard de Docker Compose.

### Préparation

1. Vérifier que Dokploy est en version `v0.30.10`.
2. Provisionner un PostgreSQL et sa base vide.
3. Créer un réseau Docker privé partagé avec PostgreSQL, par exemple
   `gta-database-network`. Ne pas utiliser l'option Docker `--internal` : le
   publisher doit résoudre le DNS et sortir en HTTPS vers Entra ID et Graph.
4. Vérifier l'existence du réseau Dokploy, généralement
   `dokploy-network`.
5. Préparer sur l'hôte un bundle de CA lisible par l'UID/GID `10001` des
   conteneurs, typiquement en mode `0644`. Il doit contenir les
   autorités publiques usuelles et les CA internes requises par PostgreSQL,
   GLPI, Transcribe AI, Entra ID ou Graph.
6. Déposer sur l'hôte deux fichiers distincts et lisibles par l'UID/GID `10001` :
   la clé privée PEM du publisher et son certificat public X.509 PEM. Charger
   ce certificat public exact dans **Certificates & secrets** de l'application
   Entra ; la clé privée ne doit jamais être intégrée à l'image ou au dépôt.
7. Créer dans Dokploy un service **Docker Compose** depuis ce dépôt et utiliser
   le chemin `./infra/docker/apps/docker-compose.prod.yml`.
8. Copier toutes les variables de
   [infra/docker/apps/.env.example](infra/docker/apps/.env.example) dans l'onglet
   **Environment**, puis remplacer les valeurs `CHANGE_ME`.
9. Attacher PostgreSQL au réseau indiqué par `DATABASE_NETWORK_NAME`.
10. Ajouter au service `web` un domaine Dokploy pointant vers le port
   conteneur `8000`, puis limiter son accès au réseau interne.

`DATABASE_URL` doit utiliser le nom ou l'adresse joignable depuis le réseau
Docker et ne doit jamais viser `localhost`, qui désignerait le conteneur
lui-même. `PUBLIC_BASE_URL` doit être l'URL réellement accessible depuis les
postes qui consulteront le lien du ticket.

`CA_BUNDLE_HOST_PATH` est un chemin absolu sur l'hôte Docker, et non dans le
conteneur. Le fichier est monté en lecture seule dans les applications sous
`/etc/ssl/certs/gta-ca-bundle.pem`, puis utilisé par HTTPX et Psycopg. Ne
désactivez pas la vérification TLS.

`PUBLISHER_ENTRA_PRIVATE_KEY_PEM_HOST_PATH` et
`PUBLISHER_ENTRA_CERTIFICATE_PEM_HOST_PATH` sont eux aussi des chemins absolus
sur l'hôte. Le second fichier doit contenir un certificat X.509
`BEGIN CERTIFICATE`, pas une clé publique brute. Une passphrase éventuelle est
fournie séparément par `PUBLISHER_ENTRA_PRIVATE_KEY_PASSPHRASE`.

Le publisher doit pouvoir joindre `login.microsoftonline.com` et
`graph.microsoft.com` en HTTPS. Côté Microsoft 365, configurez Exchange Online
Application RBAC avec `Application Mail.ReadWrite`, limité à la boîte dédiée,
puis validez le scope avec `Test-ServicePrincipalAuthorization`. La procédure
complète est décrite dans le
[guide Microsoft](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac).
`PUBLISHER_MAILBOX_ID` attend l'Object ID Entra de l'utilisateur boîte, et non
son Exchange GUID.

Le web n'expose aucun port hôte : Dokploy configure son Traefik sur le port
interne `8000`. Le volume audio est monté en lecture-écriture dans le publisher
et le worker de rétention, et en lecture seule dans le web et le worker
d'automatisation.

Le contrat Compose peut être vérifié sans démarrer les services :

```bash
docker compose \
  --env-file ./infra/docker/apps/.env.example \
  --file infra/docker/apps/docker-compose.prod.yml \
  config --quiet
```

Ne lancez jamais `docker compose down -v` sur cet environnement : l'option
`-v` supprimerait le volume audio. La sauvegarde et la restauration de
PostgreSQL et du volume audio doivent être organisées en dehors de
l'application.

## Politique de tests

La suite locale est déterministe et n'appelle aucun service externe. Les
horloges, fonctions de sommeil, clients et repositories sont injectés lorsque
c'est nécessaire ; les tests ne dorment pas réellement. Les contrats Graph et
le publisher utilisent un faux serveur HTTP stateful lié à `127.0.0.1`.

Les suites d'intégration sont opt-in :

| Marqueur | Environnement |
| --- | --- |
| `database` | PostgreSQL 16 jetable : migrations, schéma, repositories, verrous et cycles applicatifs |
| `deployment` | Construction et validation du Compose et des cinq images |
| `glpi` | GLPI 11.0.7 et MySQL 8.4.11 jetables, OAuth et contrats réels |
| `transcribe_ai` | Stack Transcribe AI v1 jetable, CPU par défaut ou GPU explicite |
| `e2e` | Pipeline aval avec frontière publisher synthétique, web, automation, retention, PostgreSQL, GLPI et Transcribe AI |

Les suites Docker sont séquentielles : n'utilisez pas `pytest-xdist`, l'option
`-n` ou plusieurs suites lourdes en parallèle. Les fixtures attendent les
services par polling et détruisent leurs conteneurs, réseaux et volumes
jetables après la session.

L'E2E aval ne démarre pas le publisher réel et ne nécessite donc aucun tenant
Entra ID. Un helper test-only produit sa frontière durable — audio atomique,
job et événement `job_created` attribué à `worker_publisher` — tandis que les
tests du publisher couvrent séparément l'authentification injectée, Graph, les
dossiers `Processed`/`Rejected` et la reprise.

### Commandes de validation depuis la racine

Suite locale sans service externe :

```bash
uv run --all-packages pytest \
  -m "not database and not deployment and not glpi and not transcribe_ai and not e2e"
```

Qualité :

```bash
uv run ruff format --check .
uv run ruff check .
```

Intégration PostgreSQL :

```bash
uv run --all-packages pytest -m database -vv --maxfail=1
```

Validation du packaging de déploiement :

```bash
uv run pytest -m deployment tests/deployment -vv --maxfail=1
```

Intégration GLPI :

```bash
uv run --all-packages pytest -m glpi \
  packages/glpi-client/tests/integration/glpi \
  apps/worker-retention/tests/integration/glpi \
  -vv --maxfail=1
```

Intégration Transcribe AI sur CPU :

```bash
TRANSCRIBE_AI_TEST_DEVICE=cpu \
uv run --all-packages pytest -m transcribe_ai -vv --maxfail=1
```

Recette E2E de release sur CPU :

```bash
TRANSCRIBE_AI_TEST_DEVICE=cpu \
uv run pytest -m e2e tests/e2e -vv --maxfail=1
```

Le passage CPU est obligatoire. Le GPU constitue une validation optionnelle :

```bash
TRANSCRIBE_AI_TEST_DEVICE=gpu \
uv run pytest -m e2e tests/e2e -vv --maxfail=1
```

Le [runbook de démonstration](docs/mvp-demo-runbook.md) décrit une recette
manuelle bornée. La [documentation E2E](tests/e2e/README.md) détaille la
topologie, les prérequis et le nettoyage.

## Release

Le dépôt utilise des tags Git immuables `mvp-vX.Y.Z`. Le tag `mvp-v0.1.1` est
la dernière baseline publiée ; `poc-v0.1.0` reste une archive historique et ne
doit pas être redéployé comme MVP.

Avant une release, fusionnez les changements validés dans `main`, puis
synchronisez votre copie locale :

```bash
git switch main
git pull --ff-only
git status --short
```

La copie doit être propre. Exécutez ensuite les commandes de validation
ci-dessus — dont l'E2E CPU obligatoire — puis créez le tag annoté sans modifier
la révision testée :

```bash
git tag -a mvp-vX.Y.Z -m "release: mvp vX.Y.Z"
git push origin mvp-vX.Y.Z
```

Dans Dokploy, déployez ce tag exact, jamais `latest` ni une branche mutable.
Un rollback consiste à redéployer le tag MVP précédemment validé. Les notes de
release doivent résumer les changements fonctionnels, les migrations et les
limites connues.

## Limites connues avant une production renforcée

- aucune authentification ni autorisation applicative sur le web ;
- les rejets d'ingestion Exchange sont conservés en base mais n'ont pas encore
  de liste ou de détail dédiés dans le dashboard ;
- transcription non persistée et absence d'idempotence Transcribe AI, avec
  risque de retranscription ou de job distant orphelin ;
- unicité de `external_id` non garantie à distance par GLPI ; une création au
  résultat inconnu peut encore nécessiter une décision opérateur ;
- un seul worker de chaque type, sans haute disponibilité ni parallélisation ;
- ingestion Exchange par polling paginé, sans abonnement Graph ni delta query ;
- exactement une pièce jointe WAV ou OGG d'au plus 25 Mio acceptée par message ;
- alertes limitées au dashboard, sans historique, acquittement ou notification
  externe ;
- fichiers audio orphelins d'une ingestion interrompue non nettoyés ;
- heartbeats susceptibles de paraître périmés pendant un appel HTTP synchrone
  long ;
- chiffrement au repos, coffre de secrets et stratégie de sauvegarde/restauration
  hors périmètre ;
- rôle PostgreSQL encore partagé entre la migration et les runtimes ;
- tests E2E sans navigateur réel et sans validation du Traefik ou du TLS
  Dokploy ;
- images construites sur le serveur, sans promotion par digest, signature ou
  SBOM ;
- aucune migration automatique des données ou volumes du POC.

La conception détaillée du système courant est disponible dans
[docs/mvp-architecture.md](docs/mvp-architecture.md).
