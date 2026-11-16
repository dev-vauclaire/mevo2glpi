# Étape 3 — Démarrer PostgreSQL et migrer le schéma

## Objectif

Démarrer la source de vérité applicative locale puis appliquer toutes les
migrations Alembic avant les services persistants.

## Démarrer PostgreSQL

```bash
docker compose --file infra/docker/development/docker-compose.postgres.yml up --detach --wait
```

PostgreSQL 18 écoute uniquement sur `127.0.0.1:5432` et persiste ses données
dans le volume `mevo2glpi-development-postgres18-data`.

Si le port est déjà utilisé, adapter ensemble :

```dotenv
MEVO2GLPI_DEV_POSTGRES_PORT=15432
DATABASE_URL=postgresql+psycopg://mevo2glpi_dev:mevo2glpi_dev_only@127.0.0.1:15432/mevo2glpi_dev
```

Ne jamais rattacher directement à PostgreSQL 18 un volume créé par une version
majeure antérieure sans migration explicite.

## Exécuter la migration one-shot

```bash
uv run --package mevo2glpi-database-migrations mevo2glpi-database-migrations
```

La commande doit terminer avec le code `0` avant de lancer le web ou un worker.

## Validation

```bash
docker compose --file infra/docker/development/docker-compose.postgres.yml ps
```

Le conteneur `postgres` doit être `healthy`. Une nouvelle exécution de la
migration doit réussir sans modifier le schéma, ce qui valide son idempotence.

En cas d'échec, contrôler d'abord que le port publié correspond à l'hôte de
`DATABASE_URL`, puis lire les logs :

```bash
docker compose --file infra/docker/development/docker-compose.postgres.yml logs postgres
```

## Étape suivante

- pour lancer le publisher : [Microsoft Graph simulé](04-microsoft-graph-simulator.md) ;
- pour le dashboard seul : aller directement à
  [l'authentification](06-authentification.md).
