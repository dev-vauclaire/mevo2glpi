# Étape 10 — Arrêter ou réinitialiser l'environnement

## Objectif

Arrêter proprement les processus et distinguer un arrêt réversible d'une
suppression irréversible des données locales.

## Arrêt non destructif

Interrompre les quatre processus uv avec `Ctrl+C`, puis arrêter les services
Docker utilisés :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml down
docker compose --env-file infra/docker/development/keycloak.env --file infra/docker/development/docker-compose.keycloak.yml down
docker compose --file infra/docker/development/docker-compose.postgres.yml down
```

Ignorer la commande Keycloak si le laboratoire n'a pas été démarré. Les
volumes PostgreSQL, GLPI, MySQL et Keycloak sont conservés.

## Redémarrage

Reprendre les étapes nécessaires dans leur ordre : PostgreSQL, migrations,
dépendances optionnelles, puis applications. Les données locales doivent être
retrouvées.

## Réinitialisation de GLPI et des services de test

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml down --volumes
```

Cette commande supprime le GLPI, sa base MySQL et leurs données locales. Elle
ne touche pas PostgreSQL applicatif ni Keycloak.

## Réinitialisation de Keycloak

```bash
docker compose --env-file infra/docker/development/keycloak.env --file infra/docker/development/docker-compose.keycloak.yml down --volumes
```

Le realm, les clients, utilisateurs, rôles et sessions sont supprimés. Au
prochain démarrage, le realm versionné est réimporté.

## Réinitialisation de PostgreSQL applicatif

Une suppression du volume PostgreSQL détruit tous les jobs, événements,
comptes locaux et sessions. Elle n'est justifiée que pour repartir d'une base
de développement neuve. Afficher d'abord le nom exact du projet et de ses
volumes :

```bash
docker compose --file infra/docker/development/docker-compose.postgres.yml ps
docker volume ls --filter name=mevo2glpi-development-postgres18-data
```

Utiliser ensuite `down --volumes` uniquement après avoir confirmé que la cible
est bien l'environnement local. Ne jamais exécuter ces commandes contre un
projet de production.

## Nettoyage des fichiers locaux

`.env`, `infra/docker/development/keycloak.env`, les audios et la clé locale
peuvent être supprimés lorsqu'ils ne sont plus nécessaires. Ils ne sont pas
récupérables depuis Git.

## Résultat attendu

Après un arrêt non destructif, aucun processus mevo2glpi ne tourne mais les
données restent disponibles. Après une réinitialisation explicitement choisie,
seuls les volumes locaux visés sont supprimés.
