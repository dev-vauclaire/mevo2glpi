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

## Tests

Les tests unitaires ne nécessitent pas PostgreSQL :

```bash
uv run pytest apps/database-migrations/tests/unit
```

La suite marquée `database` démarre une seule image PostgreSQL 16 épinglée par
digest. Elle l'expose sur un port aléatoire limité au loopback, crée une base
isolée par test, vérifie la migration et les contraintes réelles, puis détruit
le conteneur et son réseau. Elle ne doit pas être parallélisée :

```bash
uv run pytest -m database \
  apps/database-migrations/tests/integration -vv --maxfail=1
```
