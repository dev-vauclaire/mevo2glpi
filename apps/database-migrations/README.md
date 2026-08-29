# Application database-migrations

Cette application uv exécute les migrations Alembic du MVP puis se termine.
Elle ne sert aucune requête et ne possède pas les modèles SQLAlchemy : ceux-ci
restent dans le package partagé `glpi-ticketing-database`.

## Contrat d'exécution

La variable `DATABASE_URL` est obligatoire et doit utiliser le dialecte
synchrone `postgresql+psycopg`. L'application tente de joindre PostgreSQL
pendant la fenêtre de readiness, exécute `upgrade head`, puis retourne `0`.
Après l'expiration de cette fenêtre, une tentative de connexion déjà engagée
peut encore durer jusqu'au timeout de connexion configuré. Une configuration
invalide, une base indisponible ou une migration en échec produit un code non
nul sans journaliser l'URL, son mot de passe ou le détail brut de l'exception.

Variables optionnelles :

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `DATABASE_READY_TIMEOUT_SECONDS` | `60` | Fenêtre pendant laquelle relancer PostgreSQL |
| `DATABASE_READY_INTERVAL_SECONDS` | `1` | Intervalle entre deux essais |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | `5` | Limite de chaque connexion, entre 1 et 60 secondes |

Lancement depuis la racine du workspace :

```bash
uv run glpi-ticketing-database-migrations
```

La forme module est équivalente :

```bash
uv run python -m glpi_ticketing_database_migrations
```

Les ressources Alembic sont embarquées dans le package Python. Leur résolution
ne dépend donc pas du répertoire courant du processus.

## Image et déploiement

Le [Dockerfile de migration](../../infra/docker/database-migrations/Dockerfile)
construit une image multi-stage verrouillée par `uv.lock`, sans dépendances de
développement ni installation editable. Le runtime non-root exécute uniquement
`glpi-ticketing-database-migrations`, puis s'arrête : le service Compose est un
one-shot sans healthcheck ni politique de redémarrage.

Cette application accède seulement à PostgreSQL. Elle ne monte pas le volume
audio `/data/audios`. PostgreSQL doit être healthy avant son lancement et sa
réussite conditionne le démarrage du web et des deux workers. Les commandes et
fichiers propres aux environnements dev, staging et production sont décrits
dans le [runbook Docker Compose](../../infra/docker/README.md).

## Historique courant

- `0001_initial_schema` crée les quatre tables du cycle de vie MVP ;
- `0002_add_operator_retry_event` étend la contrainte des événements avec
  `operator_retry_requested`, sans réécrire la migration initiale ;
- `0003_add_retention_lifecycle` remplace le libellé GLPI par son identifiant
  numérique, ajoute les invariants du cycle de purge et autorise les quatre
  événements du worker de rétention ;
- `0004_add_observability_queries` ajoute l'index de lecture des rétentions par
  état et impose la cohérence entre état runtime et erreur expurgée.

Les montées depuis `0001` et `0002` sont testées avec des données
préexistantes. `0003` convertit explicitement les valeurs numériques de
`last_glpi_status` vers un `SMALLINT` positif et son downgrade restaure leur
représentation texte. Une ancienne valeur non vide et non numérique fait
échouer la migration au lieu d'être perdue silencieusement.

Les downgrades des migrations d'événements conservent volontairement la
sécurité native de PostgreSQL : ils échouent si des événements plus récents
existent encore, au lieu de supprimer silencieusement l'historique d'audit.
De même, `0004` refuse une donnée runtime antérieure incohérente au lieu de
masquer ou réécrire son erreur pendant la montée de version.

## Tests

Les tests unitaires ne nécessitent pas PostgreSQL :

```bash
uv run pytest apps/database-migrations/tests/unit
```

La fixture commune `tests/integration/postgres` démarre une seule image
PostgreSQL 16 épinglée par digest. Elle l'expose sur un port aléatoire limité au
loopback, crée une base isolée par test, vérifie la migration et les contraintes
réelles, puis détruit le conteneur et son réseau. Elle est partagée avec les
tests d'ingestion du web et ne doit pas être parallélisée :

```bash
uv run pytest -m database \
  apps/database-migrations/tests/integration \
  apps/web/tests/integration/test_job_ingestion.py \
  -vv --maxfail=1
```
