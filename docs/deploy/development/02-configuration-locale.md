# Étape 2 — Préparer la configuration locale

## Objectif

Installer le workspace verrouillé, créer la configuration locale et préparer
les répertoires qui ne doivent jamais entrer dans Git.

## Préparer le workspace

Depuis la racine du dépôt :

```bash
uv sync --locked --all-packages --all-groups
cp .env.example .env
mkdir -p infra/docker/runtime/audios
mkdir -p infra/docker/runtime/secrets
umask 077
openssl rand -base64 32 > infra/docker/runtime/secrets/originating-phone-number.key
```

`.env.example` contient uniquement des valeurs synthétiques et des URL
loopback. `.env`, les fichiers sous `infra/docker/runtime/`, les certificats et
les clés restent locaux et ne doivent jamais être committés.

## Variables communes

Conserver les valeurs suivantes pour distinguer clairement l'instance locale :

```dotenv
APP_ENVIRONMENT=development
APP_RELEASE=development
APP_COMMIT_SHA=unknown
AUDIO_ROOT=./infra/docker/runtime/audios
PUBLIC_BASE_URL=http://localhost:8000
ORIGINATING_PHONE_NUMBER_KEY_PATH=./infra/docker/runtime/secrets/originating-phone-number.key
```

La clé du numéro d'origine doit être lue par le publisher et automation. Une
rotation rend les valeurs déjà chiffrées indéchiffrables ; ne pas la remplacer
au milieu d'un scénario en cours.

## Choisir les composants

Ne compléter que les variables du profil retenu :

- `WEB_AUTH_*` pour le mode local ou OIDC ;
- `PUBLISHER_*` pour le simulateur Graph ;
- `OPENAI_COMPATIBLE_TRANSCRIPTION_*` pour automation ;
- `GLPI_*` pour automation et retention.

Les étapes suivantes donnent les valeurs cohérentes avec chaque service local.
Ne jamais contourner une erreur TLS, OIDC ou d'authentification en désactivant
un contrôle de sécurité.

## Validation

```bash
test -f .env
test -s infra/docker/runtime/secrets/originating-phone-number.key
git status --short
```

Le fichier `.env` et la clé ne doivent pas apparaître dans la sortie Git.

Valider également les recettes sans démarrer de conteneur :

```bash
docker compose --file infra/docker/development/docker-compose.postgres.yml config --quiet
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml config --quiet
docker compose --env-file infra/docker/development/keycloak.env.example --file infra/docker/development/docker-compose.keycloak.yml config --quiet
```

## Étape suivante

Démarrer [PostgreSQL et les migrations](03-postgresql.md).
