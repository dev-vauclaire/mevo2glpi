# Application web

`glpi-ticketing-web` reçoit les audios et crée leur état persistant initial.
Sur la branche MVP, PostgreSQL est l'unique source de vérité : le web ne déduit
plus jamais l'état d'un job en parcourant des répertoires.

Le tag `poc-v0.1.0` conserve l'ancienne application fondée sur `pending` et
`completed`. Le worker de la branche courante ne basculera vers PostgreSQL qu'à
l'itération 3 ; le Compose MVP complet appartient à une itération ultérieure.

## Entrées et sorties

| Route | Entrée | Sortie nominale |
| --- | --- | --- |
| `GET /health` | aucune | `200`, liveness du processus uniquement |
| `GET /` | aucune | formulaire HTML réutilisable |
| `POST /api/jobs` | multipart `audio_file` | `202`, UUID, `state=pending` et `Location` |
| `POST /audios` | multipart `audio_file` | façade HTML temporaire utilisant le même cas d'usage |
| `GET /api/jobs/{uuid}` | UUID canonique | état et étape persistés, ou `404` |
| `GET /audios/{uuid}` | UUID canonique | audio uniquement si PostgreSQL indique `completed` |

Exemple de réponse :

```json
{
  "audio_uuid": "8ce23f5b-4e31-49f1-ae23-4eacdbba48f8",
  "state": "pending"
}
```

`Location` vaut `/api/jobs/{audio_uuid}` et désigne donc immédiatement une
ressource consultable. Les extensions acceptées sont `wav`, `mp3`, `ogg` et
`m4a`. L'extension du nom est validée, mais le contenu binaire et le type MIME
ne sont pas encore inspectés.

Erreurs principales :

- `413` : limite de taille dépassée ;
- `415` : extension inconnue ;
- `422` : champ absent ou fichier vide ;
- `409` : collision exceptionnelle de destination ;
- `503` : stockage ou PostgreSQL temporairement indisponible.

Les réponses et les logs n'incluent ni URL SQL, ni chemin absolu, ni contenu
audio, ni message brut provenant d'une exception d'infrastructure.

## Flux d'ingestion

```mermaid
flowchart LR
    Client[Client] --> Validate[Valider extension et taille]
    Validate --> Part[Écrire un .part par blocs]
    Part --> Publish[fsync puis publication create-only]
    Publish --> Audio[(Volume /data/audios)]
    Audio --> Tx[Transaction PostgreSQL]
    Tx --> Job[Job pending]
    Tx --> Event[Événement job_created / web]
    Tx --> Commit[Commit]
    Commit --> Accepted[202 + Location]
    Tx -->|échec confirmé| Delete[Compensation de l'audio]
    Commit -->|résultat incertain| Reconcile[Relire le job par UUID]
```

L'audio est publié et synchronisé avant le premier `INSERT`. Le worker ne
consommera que des lignes PostgreSQL : un crash entre le fichier et la
transaction peut produire un orphelin, mais jamais un job pointant vers un
fichier encore partiel.

Une erreur pendant `commit()` est ambiguë : le serveur peut avoir validé la
transaction alors que la connexion a été perdue avant l'accusé de réception.
Le web ouvre donc une nouvelle session et relit l'UUID :

- ligne cohérente : le dépôt est accepté et l'audio reste en place ;
- ligne absente ou PostgreSQL toujours indisponible : l'issue reste ambiguë et
  l'audio est conservé comme orphelin non consommable. Une lecture négative
  immédiate ne prouve pas que le serveur n'achèvera pas encore le `COMMIT`.

Seule une erreur obtenue avant l'envoi du `COMMIT`, suivie d'un rollback, permet
une compensation immédiate. Cette politique privilégie un orphelin réconciliable
à un job `pending` dont les octets auraient été supprimés.

La maintenance différée de ces orphelins appartient au worker de rétention.

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
| `controllers` | Prévalider le transport, traduire les erreurs, puis construire statut, en-têtes, corps ou template |
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
│   ├── services/            # use cases indépendants de FastAPI
│   ├── schemas/             # contrats HTTP Pydantic
│   ├── middlewares/         # traitements HTTP transversaux futurs
│   ├── templates/           # pages HTML Jinja
│   └── static/
│       ├── css/             # présentation visuelle
│       └── js/              # interactions strictement côté navigateur
└── tests/
    ├── unit/                # couches, dépendances, contrats et configuration
    └── integration/         # Uvicorn et ingestion PostgreSQL réelle
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

## Configuration

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `DATABASE_URL` | obligatoire | URL `postgresql+psycopg`, traitée comme secret |
| `AUDIO_ROOT` | `/data/audios` | volume canonique existant et accessible |
| `WEB_HOST` | `127.0.0.1` | adresse IP d'écoute |
| `WEB_PORT` | `8000` | port Uvicorn |
| `WEB_TRUST_PROXY_HEADERS` | `false` | confiance explicite dans le reverse proxy |
| `MAX_UPLOAD_BYTES` | `26214400` | limite par fichier |

Le moteur est construit de façon paresseuse avec `pool_pre_ping`. `/health`
reste une sonde de liveness et ne dépend pas de PostgreSQL ; une readiness
complète sera ajoutée avec l'observabilité du MVP.

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
  apps/web/tests/integration/test_job_ingestion.py -vv --maxfail=1
```

Qualité :

```bash
uv run ruff format --check apps/web
uv run ruff check apps/web
```

## Limites actuelles

- pas encore de liste paginée, dashboard, lot navigateur ou retry opérateur ;
- pas d'authentification applicative ;
- pas de détection du format réel de l'audio ;
- la limite applicative est vérifiée pendant la lecture de `UploadFile`, donc
  après le parsing multipart de Starlette ; une exposition réseau doit imposer
  aussi une limite de corps en amont, par exemple avec Nginx ;
- pas d'idempotence HTTP : un retry après perte de la réponse peut créer un
  second UUID ;
- pas encore de suppression des orphelins ;
- téléchargement après purge non distingué de `404` avant l'itération 4 ;
- Compose MVP PostgreSQL non encore assemblé.
