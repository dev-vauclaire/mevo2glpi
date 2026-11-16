# Étape 9 — Valider l'environnement

## Objectif

Vérifier le scénario local choisi avec des données exclusivement synthétiques.

## Contrôles communs

```bash
docker compose --file infra/docker/development/docker-compose.postgres.yml ps
curl --fail http://127.0.0.1:8000/health
```

Le dashboard doit afficher `development`. Les workers lancés doivent publier
un heartbeat récent ; les workers volontairement absents ne valident pas un
pipeline complet.

## Dashboard minimal

1. se connecter avec le compte local ou OIDC ;
2. vérifier les listes de jobs et de messages rejetés ;
3. vérifier qu'un utilisateur OIDC sans rôle reçoit `403` ;
4. se déconnecter et vérifier que la session n'est plus utilisable.

## Pipeline complet

Injecter un message synthétique :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml exec microsoft-graph-simulator mevo2glpi-microsoft-graph-simulator-admin seed --scenario valid-wav
```

Contrôler ensuite :

1. le publisher découvre le message et crée une ingestion ;
2. le job passe de `pending` à `processing` ;
3. automation appelle le provider configuré ;
4. un ticket est créé dans le GLPI local avec la source `MEVO` ;
5. le job passe à `completed` et l'audio reste disponible selon la politique
   de rétention ;
6. aucun secret ni transcription complète n'apparaît dans les logs.

La commande suivante injecte tous les cas synthétiques documentés par le
simulateur pour une revue manuelle du dashboard :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml exec microsoft-graph-simulator mevo2glpi-microsoft-graph-simulator-admin seed-suite
```

Vérifier notamment les pièces jointes absentes, multiples, vides, corrompues
ou non prises en charge dans la vue des messages rejetés.

## Contrôles qualité du dépôt

```bash
uv run ruff format --check .
uv run ruff check .
uv run --all-packages pytest apps packages tests -m "not database and not deployment and not glpi and not transcription_model and not e2e" --maxfail=1
uv run pytest providers --maxfail=1
uv run --all-packages pytest -m database -vv --maxfail=1
```

Les tests `database` et `glpi` créent leurs propres environnements jetables et
ne doivent pas manipuler les volumes locaux de cette procédure.

## Résultat attendu

Le profil sélectionné est reproductible après redémarrage non destructif. Un
pipeline déclaré « complet » doit couvrir Graph, transcription, GLPI et le
dashboard sans contacter un service de production.

## Étape suivante

À la fin de la session, appliquer la procédure
[d'arrêt ou de réinitialisation](10-arret-et-reinitialisation.md).
