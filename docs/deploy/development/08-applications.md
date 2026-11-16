# Étape 8 — Lancer les applications

## Objectif

Lancer uniquement les processus utiles au profil choisi, après succès des
migrations et disponibilité de leurs dépendances.

## Dépendances par processus

| Processus | Dépendances nécessaires |
| --- | --- |
| Web | PostgreSQL et authentification locale ou OIDC |
| Publisher | PostgreSQL et Microsoft Graph simulé |
| Automation | PostgreSQL, transcription et GLPI |
| Retention | PostgreSQL et GLPI |

Lancer chaque processus dans un terminal séparé :

```bash
uv run --package mevo2glpi-web mevo2glpi-web
uv run --package mevo2glpi-worker-publisher mevo2glpi-worker-publisher
uv run --package mevo2glpi-worker-automation mevo2glpi-worker-automation
uv run --package mevo2glpi-worker-retention mevo2glpi-worker-retention
```

`--package` sélectionne le membre du workspace uv qui fournit les dépendances
et le script console. Le dernier argument est le nom du script déclaré par ce
membre.

Le minimum pour développer le dashboard est :

```bash
uv run --package mevo2glpi-web mevo2glpi-web
```

Le pipeline complet exige les quatre commandes.

## Validation immédiate

```bash
curl --fail http://127.0.0.1:8000/health
```

Ouvrir ensuite `http://127.0.0.1:8000/` et se connecter avec le mode préparé à
l'étape 6. `/health` reste public et ne valide pas l'authentification de
l'opérateur.

Une seconde instance d'un même worker est refusée par son verrou PostgreSQL et
se termine avec le code `3`.

## Diagnostic

- vérifier que la migration a terminé avec le code `0` ;
- contrôler les URLs et ports dans `.env` ;
- démarrer une dépendance absente plutôt que désactiver les retries ou TLS ;
- les logs ne doivent contenir ni transcription, ni mot de passe, ni clé API ;
- `DEBUG` sert uniquement à une investigation bornée puis doit revenir à
  `INFO`.

## Étape suivante

Exécuter la [validation fonctionnelle](09-validation.md) adaptée au profil.
