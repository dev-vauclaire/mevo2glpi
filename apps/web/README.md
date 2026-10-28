# Application web

`glpi-ticketing-web` expose le tableau de bord interne du MVP. L'ingestion des
messages Exchange et la publication des audios appartiennent au
`worker-publisher` : le web ne crée plus de job par upload. PostgreSQL est
l'unique source de vérité et le web ne déduit jamais l'état d'un job en
parcourant le volume audio.

Le tag `poc-v0.1.0` conserve l'ancienne application fondée sur `pending` et
`completed`. La branche courante utilise le stockage canonique et PostgreSQL ;
elle est assemblée dans la stack Docker Compose du MVP.

## Entrées et sorties

| Route | Entrée | Sortie nominale |
| --- | --- | --- |
| `GET /health` | aucune | `200`, liveness du processus uniquement |
| `GET /` | aucune | dashboard interne en consultation |
| `GET /api/jobs` | `state`, UUID exact, `limit`, curseur optionnels | page triée par dernière transition décroissante |
| `GET /api/jobs/{uuid}` | UUID canonique | détail persistant et rétention, ou `404` |
| `GET /api/jobs/{uuid}/events` | `limit` et curseur optionnels | historique expurgé, paginé séparément |
| `POST /api/jobs/{uuid}/retry` | UUID d'un job `failed` ou `blocked` | `202`, état `pending` et `Location` |
| `GET /api/observability` | aucune | état des trois workers et alertes opérationnelles agrégées |
| `GET /audios/{uuid}` | UUID canonique | audio `completed`, `410` si purgé, sinon `404` |

La collection `/api/jobs` est volontairement en lecture seule : un `POST`
retourne `405 Method Not Allowed`. La pagination utilise un curseur opaque fondé
sur `(updated_at, uuid)` ; le client ne doit ni l'interpréter ni le fabriquer.

Le dashboard lit séparément l'état des jobs et l'instantané d'observabilité.
Il affiche toujours une carte pour chacun des workers `publisher`, `automation`
et `retention`, même si aucun heartbeat n'a encore été publié. La carte du
publisher expose son UUID d'ingestion mail courant, lorsqu'il existe. Les
alertes jobs et rétentions sont agrégées par condition :
leur compteur donne le nombre de lignes concernées et un bouton applique le
filtre d'état correspondant lorsque celui-ci existe. Le navigateur remplace
l'instantané à chaque lecture ; il ne conserve ni historique d'alerte ni
acquittement local.

La relance opérateur est autorisée uniquement lorsque le job est `failed` ou
`blocked`, que son fichier canonique existe encore et que sa taille correspond
aux métadonnées persistées. Une transaction verrouillée réarme le job, rend sa
rétention `inactive` et ajoute `operator_retry_requested`. Elle conserve
notamment `glpi_ticket_id` : si cet ID existe déjà, le worker finalise ensuite
localement sans rappeler GLPI.

Une perte de l'accusé de réception du `COMMIT` de relance est réconciliée dans
une nouvelle session : job `pending`, rétention `inactive` et événement
opérateur doivent être tous les trois présents. Le web retourne alors `202` ;
une issue encore ambiguë reste un `503` et n'entraîne jamais un second
réarmement aveugle.

Erreurs principales :

- `422` : filtre ou curseur invalide ;
- `404` : job inconnu ou audio non terminé ;
- `409` : état ou audio incompatible avec une relance opérateur ;
- `410` : audio terminé déjà en suppression ou supprimé ;
- `500` : ligne `completed` incohérente avec sa rétention ou son stockage ;
- `503` : stockage ou PostgreSQL temporairement indisponible.

Les réponses et les logs n'incluent ni URL SQL, ni chemin absolu, ni contenu
audio, ni message brut provenant d'une exception d'infrastructure. Un audio
autorisé est servi comme pièce jointe sous son nom UUID canonique, avec
`Cache-Control: private, no-store` afin d'éviter sa conservation par un cache
HTTP partagé ou par le navigateur.

## Image et stockage Docker

Le [Dockerfile web](../../infra/docker/apps/web/Dockerfile) produit une image
multi-stage non-root contenant uniquement l'environnement Python installé
depuis `uv.lock`. Dans Compose, le web monte le volume canonique
`/data/audios` uniquement pour valider les retries et servir les audios
terminés, sans déduire leur état depuis ce volume. Le code web ne requiert plus
de droit d'écriture pour une ingestion.

Le healthcheck de l'image appelle `/health`, qui reste une liveness locale et
ne transforme pas PostgreSQL en dépendance de disponibilité HTTP. Le service
web ne publie aucun port hôte ; le Traefik fourni par Dokploy route son port
interne `8000`. Le contrat de démarrage est décrit dans la section
[déploiement Dokploy](../../README.md#déploiement-dokploy).

## Flux de consultation

```mermaid
flowchart LR
    Browser[Navigateur interne] -->|GET| Routes[Routes FastAPI]
    Routes --> Queries[Services de lecture]
    Queries --> Database[(PostgreSQL)]
    Queries --> Dashboard[Jobs, événements et alertes expurgés]
    Browser -->|GET audio completed| Storage[(Volume /data/audios)]
    Browser -->|POST retry opérateur| Retry[Transaction de réarmement]
```

Les lectures ne modifient ni PostgreSQL ni le volume. L'unique commande métier
conservée est la relance opérateur d'un job `failed` ou `blocked`; elle est
auditée par l'événement `operator_retry_requested`. Les lignes
`mail_ingestions`, notamment les messages rejetés, restent persistées pour une
future vue dédiée ; cette itération n'expose pas encore leur liste.

## Architecture

La direction des appels applicatifs est volontairement unidirectionnelle :

```mermaid
flowchart LR
    Routes --> Controllers
    Controllers --> Services
    Services --> Repositories[Repositories SQLAlchemy]
    Services --> Storage[Stockage audio commun]
    Controllers --> Schemas[Schemas HTTP]
    Controllers --> Templates[Templates HTML]
    Middlewares[Middlewares HTTP] -. transversal .-> Routes
```

| Couche | Responsabilité |
| --- | --- |
| `routes` | Associer méthode, URL, paramètres FastAPI et controller, sans règle ni accès aux données |
| `middlewares` | Réserver les traitements HTTP transversaux ; aucun middleware applicatif n'est nécessaire actuellement |
| `schemas` | Définir uniquement les contrats Pydantic exposés par HTTP |
| `controllers` | Traduire le protocole HTTP et les erreurs, puis construire statut, en-têtes, corps ou template |
| `services` | Porter les use cases et orchestrer stockage, règles et transactions PostgreSQL |
| `templates` / `static` | Fournir les pages HTML et leurs assets CSS/JavaScript, sans accès aux services |

Les repositories restent dans le package partagé `database`. Ils ne connaissent
ni FastAPI ni les contrats HTTP, peuvent exécuter `flush()`, mais ne font jamais
`commit()`, `rollback()` ou `close()`.

```text
apps/web/
├── src/glpi_ticketing_web/
│   ├── application.py       # moteur, sessions, services et Uvicorn
│   ├── web.py               # assemblage FastAPI, controllers et routers
│   ├── settings.py          # configuration typée et secrets masqués
│   ├── routes/              # déclarations URL/méthode et délégation pure
│   ├── controllers/         # adaptation du protocole HTTP
│   ├── services/
│   │   ├── jobs/            # requêtes, download, retry et DTO internes
│   │   └── observability/   # seuils, priorités et projection opérationnelle
│   ├── schemas/             # contrats HTTP Pydantic
│   ├── middlewares/         # traitements HTTP transversaux futurs
│   ├── templates/           # pages HTML Jinja
│   └── static/
│       ├── css/             # présentation visuelle
│       └── js/              # interactions strictement côté navigateur
└── tests/
    ├── unit/                # couches, dépendances, contrats et configuration
    └── integration/         # Uvicorn et API sur PostgreSQL réel
```

Conventions :

- `application.py` possède le moteur SQLAlchemy et le libère à l'arrêt ;
- `web.py` assemble les couches, mais ne définit aucun endpoint ;
- une route appelle uniquement son controller ;
- un controller ne dépend ni de SQLAlchemy, ni du package commun de stockage ;
- un service ne dépend ni de FastAPI, ni de Starlette, ni de Jinja2 ;
- les enums HTTP sont possédés par `schemas` et mappés explicitement depuis
  l'état interne afin qu'une migration SQL ne modifie pas implicitement l'API ;
- une réponse de streaming ferme son descripteur dans un `finally` couvrant
  l'intégralité de son appel ASGI, y compris avant le premier octet ;
- une `Session` SQLAlchemy est l'unité de travail, sans classe `UnitOfWork` ;
- `TicketCreationJobRepository` et `TicketCreationJobEventRepository` ne
  manipulent chacun que leur modèle et font `flush()`, jamais `commit()`,
  `rollback()` ou `close()` ;
- aucun accès fichier n'est exécuté pendant la transaction SQL ;
- le nom original n'entre jamais dans `storage_key`.

L'interface utilise du JavaScript natif segmenté en `api.js`, `dashboard.js`,
`observability.js` et `app.js`. `app.js` possède l'unique
ordonnanceur de rafraîchissement : les lectures jobs et observabilité sont
attendues indépendamment, sans `setInterval`, et le polling est suspendu lorsque
l'onglet est masqué. Les valeurs venant de l'API sont ajoutées au DOM avec
`textContent` ou `replaceChildren`, jamais interprétées comme du HTML. Cette
itération suppose un accès limité au réseau interne et n'ajoute pas de mécanisme
d'authentification applicative.

## Configuration

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `DATABASE_URL` | obligatoire | URL `postgresql+psycopg`, traitée comme secret |
| `AUDIO_ROOT` | `/data/audios` | volume canonique existant et accessible |
| `WEB_HOST` | `127.0.0.1` | adresse IP d'écoute |
| `WEB_PORT` | `8000` | port Uvicorn |
| `WEB_TRUST_PROXY_HEADERS` | `false` | confiance explicite dans le reverse proxy |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | `10` | cadence attendue des checkpoints, utilisée pour valider le seuil stale |
| `DASHBOARD_WORKER_STALE_AFTER_SECONDS` | `45` | absence de checkpoint avant alerte worker |
| `DASHBOARD_PENDING_ALERT_AFTER_SECONDS` | `900` | durée depuis la dernière entrée en `pending` avant alerte |
| `DASHBOARD_RETRY_OVERDUE_GRACE_SECONDS` | `60` | grâce après l'échéance d'un retry job ou rétention |
| `DASHBOARD_RETENTION_DEADLINE_GRACE_SECONDS` | `120` | grâce après une deadline audio avant alerte critique |

Les durées doivent être positives et finies. Le seuil stale doit être
strictement supérieur à la cadence attendue du heartbeat. Le moteur est
construit de façon paresseuse avec `pool_pre_ping`. `/health` reste une sonde de
liveness indépendante de PostgreSQL ; une lecture d'observabilité impossible
retourne séparément un `503` expurgé.

## Commandes

Depuis la racine :

```bash
uv sync --all-packages --all-groups
uv run glpi-ticketing-web
```

Tests sans service externe :

```bash
uv run --all-packages pytest apps/web/tests/unit \
  apps/web/tests/integration/test_server_startup.py
```

Intégration PostgreSQL jetable, non parallélisée :

```bash
uv run pytest -m database \
  apps/web/tests/integration -vv --maxfail=1
```

Qualité :

```bash
uv run ruff format --check apps/web
uv run ruff check apps/web
```

## Limites actuelles

- pas d'authentification applicative ;
- pas d'acquittement, d'historique ou de notification externe des alertes ;
- un job `failed` ou `blocked` reste signalé jusqu'à son retry opérateur, même
  si la deadline de son audio a ensuite conduit à une purge ;
- un heartbeat `stale` indique seulement l'absence récente de checkpoint : un
  appel HTTP synchrone long peut rendre un processus vivant potentiellement non
  réactif dans le dashboard ;
- pas encore de suppression des orphelins ;
- pas encore de liste ni de détail HTTP pour `mail_ingestions` ; les rejets du
  publisher sont conservés en base mais ne sont visibles qu'indirectement via
  l'état et les alertes du worker ;
- pas encore de test E2E dans un navigateur réel ; les contrats JavaScript sont
  vérifiés statiquement et l'API est testée avec FastAPI.
