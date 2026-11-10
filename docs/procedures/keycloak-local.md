# Laboratoire Keycloak local

## Objectif et limites

Ce laboratoire permet de manipuler Keycloak depuis sa console
d'administration avant toute intégration avec mevo2glpi. Il démarre uniquement :

- Keycloak en mode de développement ;
- une base PostgreSQL dédiée à Keycloak ;
- un réseau Docker dédié et un volume persistant propres au laboratoire.

Il ne protège pas encore le dashboard mevo2glpi et ne modifie ni la base
applicative, ni les workers, ni les environnements de staging ou de production.

Tous les comptes et secrets utilisés ici sont synthétiques. Ne jamais importer
un compte, un mot de passe, un groupe ou une configuration de l'établissement.
Le mode `start-dev` et les identifiants d'exemple sont strictement réservés à la
machine locale.

## Prérequis

- Docker Engine ;
- Docker Compose ;
- les ports loopback configurés disponibles, `18084` par défaut.

Le laboratoire utilise les fichiers suivants :

```text
infra/docker/development/docker-compose.keycloak.yml
infra/docker/development/keycloak.env.example
infra/docker/development/keycloak/mevo2glpi-development-realm.json
```

## Préparer la configuration locale

Depuis la racine du dépôt :

```bash
cp infra/docker/development/keycloak.env.example infra/docker/development/keycloak.env
```

Le fichier `keycloak.env` reste local et ne doit jamais être commité. Ses
valeurs sont exclusivement destinées à cette expérimentation. Elles peuvent
être changées avant le premier démarrage.

Les variables de bootstrap de l'administrateur ne recréent pas son compte à
chaque démarrage. Une fois PostgreSQL initialisé, les comptes, realms et clients
sont lus depuis le volume persistant.

Les variables `KEYCLOAK_DEV_CLIENT_SECRET`,
`KEYCLOAK_DEV_AUTHORIZED_PASSWORD` et
`KEYCLOAK_DEV_FORBIDDEN_PASSWORD` alimentent uniquement le premier import du
realm synthétique. Elles définissent respectivement le secret du client OIDC et
les mots de passe des deux opérateurs de test. Elles ne sont jamais lues par
mevo2glpi dans ce jalon.

Valider le rendu du Compose avant son premier démarrage :

```bash
docker compose --env-file infra/docker/development/keycloak.env --file infra/docker/development/docker-compose.keycloak.yml config --quiet
```

## Démarrer Keycloak seul

```bash
docker compose --env-file infra/docker/development/keycloak.env --file infra/docker/development/docker-compose.keycloak.yml up --detach --wait
```

Contrôler les deux services :

```bash
docker compose --env-file infra/docker/development/keycloak.env --file infra/docker/development/docker-compose.keycloak.yml ps
```

Suivre les journaux si le démarrage échoue :

```bash
docker compose --env-file infra/docker/development/keycloak.env --file infra/docker/development/docker-compose.keycloak.yml logs --follow keycloak
```

PostgreSQL n'est pas publié sur l'hôte. Keycloak est accessible uniquement
depuis la machine locale :

```text
http://localhost:18084/admin
```

Utiliser le nom `localhost`, et non une adresse différente, afin de rester
aligné avec le hostname et les URL OIDC émis par Keycloak. Si le port est
modifié dans `keycloak.env`, redémarrer le laboratoire en conservant la même
valeur dans l'URL utilisée par le navigateur.

Vérifier le document de découverte du realm d'administration :

```bash
curl --fail http://localhost:18084/realms/master/.well-known/openid-configuration
```

## Comprendre les objets manipulés

### Realm

Un realm isole un ensemble d'utilisateurs, de rôles, de clients et de sessions.
Le realm `master` administre Keycloak lui-même. Il ne doit pas servir de realm
applicatif. Sur une base neuve, le laboratoire importe automatiquement le realm
suivant :

```text
mevo2glpi-development
```

### Client OIDC

Un client représente une application qui délègue l'authentification à
Keycloak. Le realm importé contient le client :

```text
mevo2glpi-web
```

Sa configuration par défaut est la suivante :

| Paramètre | Valeur d'expérimentation |
| --- | --- |
| Type de client | OpenID Connect |
| Client authentication | activé |
| Standard Flow | activé |
| Implicit Flow | désactivé |
| Direct Access Grants | désactivé |
| Service Accounts | désactivé |
| Full Scope Allowed | désactivé |
| PKCE Code Challenge Method | `S256` |
| Valid Redirect URI | `http://127.0.0.1:8000/auth/callback` |
| Web Origin | `http://127.0.0.1:8000` |
| Valid Post Logout Redirect URI | `http://127.0.0.1:8000/` |

Le client est confidentiel : son secret est injecté depuis
`KEYCLOAK_DEV_CLIENT_SECRET` pendant l'import. La valeur synthétique peut être
consultée dans `keycloak.env`, mais elle ne doit pas être réutilisée dans un
environnement partagé.

### Rôle applicatif

Le client `mevo2glpi-web` possède le rôle :

```text
mevo2glpi-access
```

Ce rôle représentera ultérieurement l'autorisation d'accéder au dashboard. Un
mapper l'expose dans la claim OIDC multivaluée `roles`. Cette configuration ne
protège encore aucune route : mevo2glpi n'est pas raccordé dans ce jalon.

### Utilisateurs synthétiques

Le realm contient deux utilisateurs sans adresse électronique ni donnée
réelle :

- `operator-authorized`, auquel le rôle client `mevo2glpi-access` est
  attribué ;
- `operator-forbidden`, sans ce rôle.

Leurs mots de passe non temporaires proviennent de `keycloak.env`. Ils servent à
vérifier séparément une authentification autorisée et une authentification qui
devra être refusée par mevo2glpi avec une réponse `403`.

Ne pas utiliser le compte administrateur du realm `master` comme utilisateur
applicatif.

## Exercices de validation

### Consulter la découverte OIDC

```bash
curl --fail http://localhost:18084/realms/mevo2glpi-development/.well-known/openid-configuration
```

Repérer notamment les propriétés suivantes :

- `issuer` : identité exacte du realm ;
- `authorization_endpoint` : endpoint utilisé par le navigateur ;
- `token_endpoint` : échange du code d'autorisation ;
- `jwks_uri` : clés publiques utilisées pour vérifier les signatures ;
- `end_session_endpoint` : déconnexion auprès de Keycloak.

L'`issuer` doit commencer exactement par :

```text
http://localhost:18084/realms/mevo2glpi-development
```

### Vérifier les comptes depuis la console utilisateur

Ouvrir :

```text
http://localhost:18084/realms/mevo2glpi-development/account/
```

Se connecter successivement avec les deux comptes synthétiques. Cette étape
vérifie leurs mots de passe, mais pas encore l'autorisation
`mevo2glpi-access`, qui sera contrôlée par l'application lors d'un futur jalon.

### Observer la persistance

Arrêter puis redémarrer le laboratoire avec les commandes non destructives de
la section suivante. Le realm, le client, le rôle et les utilisateurs doivent
toujours être présents : ils sont persistés dans PostgreSQL.

## Arrêter ou réinitialiser le laboratoire

Arrêt non destructif, en conservant toutes les manipulations :

```bash
docker compose --env-file infra/docker/development/keycloak.env --file infra/docker/development/docker-compose.keycloak.yml down
```

Redémarrage :

```bash
docker compose --env-file infra/docker/development/keycloak.env --file infra/docker/development/docker-compose.keycloak.yml up --detach --wait
```

Réinitialisation complète :

```bash
docker compose --env-file infra/docker/development/keycloak.env --file infra/docker/development/docker-compose.keycloak.yml down --volumes
```

La dernière commande supprime irréversiblement la base Keycloak locale, donc
tous les realms, clients, rôles, utilisateurs et sessions créés manuellement.
Au démarrage suivant, le realm par défaut est réimporté. Cette commande ne doit
jamais être utilisée contre un environnement partagé.

L'import au démarrage n'écrase jamais un realm déjà présent. Après les
manipulations réalisées avant l'ajout du fichier versionné, il faut donc
réinitialiser explicitement ce volume local pour éprouver la configuration par
défaut. Exporter auparavant toute expérimentation que l'on souhaite conserver.

## Diagnostic

- si le port est occupé, modifier le port publié dans `keycloak.env`, puis
  utiliser cette même valeur dans l'URL du navigateur ;
- si `keycloak-db` n'est pas sain, consulter ses logs avant ceux de Keycloak ;
- si les identifiants de bootstrap semblent ignorés après une modification,
  utiliser le compte déjà persisté ou réinitialiser explicitement le volume ;
- si le realm versionné n'apparaît pas ou ne reflète pas le fichier courant,
  vérifier si une version antérieure est déjà persistée avant de réinitialiser
  le laboratoire ;
- si les URL du document de découverte ne correspondent pas à l'URL du
  navigateur, vérifier le hostname et le port configurés avant de créer un
  client ;
- ne jamais contourner une erreur de configuration en désactivant une
  vérification de signature, d'issuer ou de redirection.

## Suite possible

Après manipulation et validation de ces notions, le prochain jalon pourra
raccorder le web mevo2glpi à ce realm avec le flux Authorization Code et PKCE.
La conception envisagée est décrite dans
[Authentification OIDC avec Keycloak](../conception/authentification-oidc-keycloak.md).
