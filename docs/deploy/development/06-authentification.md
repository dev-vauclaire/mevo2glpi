# Étape 6 — Configurer l'authentification

## Objectif

Choisir explicitement l'un des deux modes du web :

- `local`, avec un compte stocké dans PostgreSQL ;
- `oidc`, avec le laboratoire Keycloak local.

Il n'existe ni mode désactivé ni bascule automatique entre ces mécanismes.

## Option A — Authentification locale

Conserver dans `.env` :

```dotenv
WEB_AUTH_MODE=local
WEB_AUTH_SESSION_SECRET=synthetic-development-session-secret-change-before-sharing
WEB_AUTH_SESSION_MAX_AGE_SECONDS=1800
WEB_AUTH_COOKIE_SECURE=false
```

Après la migration PostgreSQL, créer un compte synthétique avec la CLI
interactive :

```bash
uv run --package mevo2glpi-web mevo2glpi-web-users create operator-local
```

Le mot de passe est demandé deux fois sans être affiché. Il doit contenir entre
15 et 128 caractères et ne doit reprendre aucun secret réel.

Commandes d'administration disponibles :

```bash
uv run --package mevo2glpi-web mevo2glpi-web-users list
uv run --package mevo2glpi-web mevo2glpi-web-users disable operator-local
uv run --package mevo2glpi-web mevo2glpi-web-users enable operator-local
uv run --package mevo2glpi-web mevo2glpi-web-users reset-password operator-local
```

La désactivation et la réinitialisation du mot de passe invalident les sessions
existantes du compte.

## Option B — Keycloak local

Le laboratoire démarre un Keycloak en mode `start-dev` et une base PostgreSQL
dédiée. Il ne partage ni base ni réseau avec mevo2glpi et ne doit jamais servir
hors de la machine locale.

### Préparer et démarrer le laboratoire

```bash
cp infra/docker/development/keycloak.env.example infra/docker/development/keycloak.env
docker compose --env-file infra/docker/development/keycloak.env --file infra/docker/development/docker-compose.keycloak.yml config --quiet
docker compose --env-file infra/docker/development/keycloak.env --file infra/docker/development/docker-compose.keycloak.yml up --detach --wait
```

Le fichier `keycloak.env` reste hors Git. Les valeurs
`KEYCLOAK_DEV_CLIENT_SECRET`, `KEYCLOAK_DEV_AUTHORIZED_PASSWORD` et
`KEYCLOAK_DEV_FORBIDDEN_PASSWORD` servent uniquement à l'import initial du
realm synthétique.

Keycloak est accessible par `http://localhost:18084`. Utiliser exactement le
nom `localhost` pour rester aligné avec son issuer.

### Objets importés

| Objet | Valeur |
| --- | --- |
| Realm | `mevo2glpi-development` |
| Client confidentiel | `mevo2glpi-web` |
| Flux | Authorization Code avec PKCE `S256` |
| Rôle applicatif | `mevo2glpi-access` |
| Claim multivaluée | `roles` |
| Utilisateur autorisé | `operator-authorized` |
| Utilisateur sans rôle | `operator-forbidden` |

Le realm `master` reste réservé à l'administration de Keycloak. Aucun compte
du realm `master` ne doit être utilisé dans mevo2glpi.

### Raccorder le web

Dans `.env` :

```dotenv
WEB_AUTH_MODE=oidc
WEB_AUTH_ISSUER_URL=http://localhost:18084/realms/mevo2glpi-development
WEB_AUTH_CLIENT_ID=mevo2glpi-web
WEB_AUTH_CLIENT_SECRET=development-only-client-secret
WEB_AUTH_REDIRECT_URI=http://127.0.0.1:8000/auth/oidc/callback
WEB_AUTH_ROLES_CLAIM=roles
WEB_AUTH_REQUIRED_ROLE=mevo2glpi-access
WEB_AUTH_SESSION_SECRET=development-only-session-secret-at-least-32-characters
WEB_AUTH_SESSION_MAX_AGE_SECONDS=1800
WEB_AUTH_COOKIE_SECURE=false
```

`WEB_AUTH_CLIENT_SECRET` doit correspondre à
`KEYCLOAK_DEV_CLIENT_SECRET` dans `keycloak.env`.

## Validation

Contrôler la découverte OIDC :

```bash
curl --fail http://localhost:18084/realms/mevo2glpi-development/.well-known/openid-configuration
```

Après lancement du web :

- `operator-authorized` accède au dashboard ;
- `operator-forbidden` s'authentifie auprès de Keycloak mais reçoit `403` ;
- en mode local, un mot de passe erroné reste refusé sans révéler si le compte
  existe.

## Diagnostic

- vérifier que l'issuer correspond exactement à l'URL ouverte dans le
  navigateur ;
- vérifier l'URI de callback, y compris le schéma, l'hôte et le port ;
- un realm déjà persisté n'est pas écrasé par un nouvel import ; une
  réinitialisation explicite peut être nécessaire après modification du JSON ;
- ne jamais désactiver la vérification de signature, d'issuer ou de rôle.

La conception complète est décrite dans
[Authentification locale ou OIDC](../../conception/authentification-oidc-keycloak.md).

## Étape suivante

Choisir le [provider de transcription](07-transcription-optionnelle.md) si le
worker d'automatisation est lancé.
