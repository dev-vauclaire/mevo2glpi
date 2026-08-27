# Package `glpi-ticketing-database`

`glpi-ticketing-database` porte les contrats SQLAlchemy partagés par le web,
le worker d'automatisation, le worker de rétention et l'application de
migration du MVP.

Le package ne contient ni point d'entrée, ni historique Alembic, ni repository
métier. L'application one-shot `apps/database-migrations` est seule responsable
de l'exécution des migrations. Les repositories et les verrous advisory seront
introduits dans les itérations où ils auront des consommateurs réels.

## Responsabilités

- déclarer les quatre tables persistantes du cycle de vie ;
- exposer `Base.metadata` à Alembic ;
- fournir un moteur SQLAlchemy synchrone limité à PostgreSQL avec Psycopg 3 ;
- fournir une fabrique de sessions synchrones ;
- centraliser les valeurs persistées et les contraintes structurelles.

PostgreSQL contient uniquement le cycle de vie et des informations
opérationnelles expurgées. Les octets audio restent dans le volume persistant et
aucune transcription, réponse HTTP brute ou valeur secrète n'est stockée dans
ces modèles.

## Tables

| Table | Rôle |
| --- | --- |
| `ticket_creation_jobs` | Source de vérité du traitement audio vers GLPI |
| `audio_retentions` | Planification et audit de la suppression des octets audio |
| `ticket_creation_job_events` | Historique append-only affiché par le dashboard |
| `worker_runtime_status` | Dernier heartbeat des deux workers singleton |

Les valeurs d'état utilisent des colonnes `VARCHAR` et des contraintes
`CHECK`, pas des enums natifs PostgreSQL. Les UUID utilisent le type UUID natif
et tous les timestamps sont des `TIMESTAMPTZ`.

`storage_key` est une clé relative canonique de la forme
`{uuid}.{extension}`. L'extension est stockée sans point et limitée à `wav`,
`mp3`, `ogg` et `m4a`. Le code consommateur résout directement
`audio_root / storage_key` et ne parcourt jamais le volume pour retrouver un
job.

Une rétention `retry_scheduled` peut représenter aussi bien un nouvel appel
GLPI qu'une reprise de purge locale. Elle possède donc toujours une date de
reprise, y compris avec la politique `deadline_only`. Seul l'état `checking`
est réservé à la politique `ticket_or_deadline`.

## Transactions

Une `Session` SQLAlchemy constitue l'unité de travail. Les applications ouvrent
des transactions courtes avec la fabrique exposée :

```python
from glpi_ticketing_database import (
    create_database_engine,
    create_session_factory,
)

engine = create_database_engine(database_url)
session_factory = create_session_factory(engine)

with session_factory.begin() as session:
    # Appeler ici un repository recevant la session.
    ...
```

Aucune transaction ne doit rester ouverte pendant un appel GLPI, Transcribe AI
ou une attente de polling.

## Configuration

L'URL doit utiliser explicitement le pilote synchrone Psycopg 3 :

```text
postgresql+psycopg://user:password@postgres:5432/glpi_ticketing
```

La fonction de création n'inclut jamais l'URL reçue dans ses erreurs de
validation. Les applications restent responsables de ne pas journaliser la
configuration brute. Le paramètre optionnel `connect_timeout_seconds` transmet
un délai de connexion à Psycopg sans l'ajouter à l'URL ; l'application de
migration le renseigne afin que chaque tentative de readiness reste bornée.

## Arborescence

```text
packages/database/
├── pyproject.toml
├── README.md
├── src/glpi_ticketing_database/
│   ├── __init__.py
│   ├── engine.py
│   └── models/
│       ├── __init__.py
│       ├── base.py
│       ├── enums.py
│       ├── ticket_creation_job.py
│       ├── audio_retention.py
│       ├── ticket_creation_job_event.py
│       └── worker_runtime_status.py
└── tests/unit/
    ├── test_engine.py
    └── test_models.py
```

## Validation depuis la racine

```bash
uv run pytest packages/database/tests/unit
uv run ruff format --check packages/database
uv run ruff check packages/database
```

Les contraintes réellement dépendantes de PostgreSQL sont couvertes par la
suite d'intégration de `apps/database-migrations`.
