# Infrastructure Docker

Ce dossier sépare les **composants réutilisables** des **recettes exécutables**.
Les chemins des recettes constituent les points d'entrée stables utilisés par
Dokploy, la CI et les procédures d'exploitation.

## Composants

Le dossier [`components/`](components/) contient les définitions partagées :

- [`application-services.yml`](components/application-services.yml) décrit le
  socle des cinq services applicatifs ;
- [`simulator-services.yml`](components/simulator-services.yml) décrit le
  build et le durcissement communs aux deux simulateurs.

Ces fichiers sont référencés avec `extends`. Ils ne déclarent pas un
environnement complet et **ne doivent jamais être déployés directement**.

## Recettes

| Environnement | Usage | Recette | Configuration | Services principaux |
| --- | --- | --- | --- | --- |
| Développement | PostgreSQL local | [`development/docker-compose.postgres.yml`](development/docker-compose.postgres.yml) | valeurs de développement intégrées | PostgreSQL |
| Développement | Laboratoire Keycloak | [`development/docker-compose.keycloak.yml`](development/docker-compose.keycloak.yml) | [`development/keycloak.env.example`](development/keycloak.env.example) | Keycloak et sa base dédiée |
| Développement | Doubles de test | [`development/docker-compose.test-services.yml`](development/docker-compose.test-services.yml) | [`.env.example`](../../.env.example) | Microsoft Graph, OpenAI-compatible et GLPI de test |
| Staging | Simulateurs | [`providers/docker-compose.staging.yml`](providers/docker-compose.staging.yml) | [`providers/staging.env.example`](providers/staging.env.example) | Microsoft Graph et OpenAI-compatible |
| Staging | GLPI optionnel | [`providers/glpi-test/docker-compose.staging.yml`](providers/glpi-test/docker-compose.staging.yml) | [`providers/glpi-test/staging.env.example`](providers/glpi-test/staging.env.example) | GLPI, MySQL et bootstrap |
| Staging | Transcription CPU | [`transcription/docker-compose.transcription.yml`](transcription/docker-compose.transcription.yml) | [`transcription/staging.env.example`](transcription/staging.env.example) | API OpenAI-compatible Parakeet |
| Staging | Application | [`apps/docker-compose.staging.yml`](apps/docker-compose.staging.yml) | [`apps/staging.env.example`](apps/staging.env.example) | migrations, web et trois workers |
| Production | Transcription CPU | [`transcription/docker-compose.transcription.yml`](transcription/docker-compose.transcription.yml) | [`transcription/production.env.example`](transcription/production.env.example) | API OpenAI-compatible Parakeet |
| Production | Application | [`apps/docker-compose.prod.yml`](apps/docker-compose.prod.yml) | [`apps/.env.example`](apps/.env.example) | migrations, web et trois workers |

Les fichiers `*.env.example` documentent le contrat de configuration. Ils ne
doivent contenir aucun secret réel.

## Ordre de démarrage

En staging :

1. créer les réseaux et démarrer PostgreSQL dans Dokploy ;
2. démarrer GLPI conteneurisé uniquement lorsqu'aucun GLPI de test externe
   n'est disponible ;
3. démarrer les simulateurs ;
4. démarrer la transcription ;
5. démarrer l'application.

En production :

1. créer les réseaux et démarrer PostgreSQL dans Dokploy ;
2. démarrer la transcription locale, si elle est utilisée ;
3. démarrer l'application.

Le service `database-migrations` termine toujours avec succès avant le
démarrage des services applicatifs persistants.

## Invariants

- Les noms de réseaux, de volumes, de services et les alias DNS restent définis
  dans les recettes d'environnement.
- Le volume audio est unique : le web et le worker d'automatisation le montent
  en lecture seule ; le publisher et le worker de rétention en lecture-écriture.
- Aucun service de staging ou de production ne publie directement un port ; le
  web est exposé par le réseau Dokploy. Les ports de développement sont liés à
  `127.0.0.1`.
- Les composants ne portent aucun secret. Les recettes et leurs fichiers
  d'environnement contrôlent les valeurs réellement déployées.
- Un changement de composant doit être validé depuis toutes les recettes qui le
  consomment.

## Validation statique

Ces commandes rendent les principales recettes sans démarrer de conteneur :

```bash
docker compose --env-file infra/docker/apps/.env.example --file infra/docker/apps/docker-compose.prod.yml config --quiet
docker compose --env-file infra/docker/apps/staging.env.example --file infra/docker/apps/docker-compose.staging.yml config --quiet
docker compose --env-file infra/docker/providers/staging.env.example --file infra/docker/providers/docker-compose.staging.yml config --quiet
docker compose --env-file .env.example --file infra/docker/development/docker-compose.test-services.yml config --quiet
docker compose --env-file infra/docker/transcription/staging.env.example --file infra/docker/transcription/docker-compose.transcription.yml config --quiet
```

Les manipulations complètes sont décrites dans les procédures de
[développement local](../../docs/procedures/developpement-local.md), de
[staging](../../docs/procedures/staging.md) et de
[production](../../docs/procedures/production.md).
