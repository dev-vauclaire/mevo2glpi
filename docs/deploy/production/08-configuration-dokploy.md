# 8. Configurer Dokploy

## Source Git

Pour chaque projet Compose :

1. sélectionner le dépôt `mevo2glpi` ;
2. sélectionner le même tag annoté `vX.Y.Z` ;
3. vérifier que Dokploy extrait le SHA consigné ;
4. désactiver l'auto-déploiement et les watch paths.

En production, ne jamais sélectionner `main`, une branche de fonctionnalité ou
une image `latest`.

## Projets à créer

| Projet Dokploy | Recette | Nécessité |
| --- | --- | --- |
| PostgreSQL | ressource Database Dokploy | obligatoire |
| Transcription | `infra/docker/transcription/docker-compose.transcription.yml` | seulement avec Parakeet local |
| Applications mevo2glpi | `infra/docker/apps/docker-compose.prod.yml` | obligatoire |

Le Compose applicatif contient `database-migrations`, `web`,
`worker-publisher`, `worker-automation` et `worker-retention`.

## Environnement du projet applicatif

Partir de [`infra/docker/apps/.env.example`](../../../infra/docker/apps/.env.example)
et copier les valeurs dans l'onglet **Environment** de Dokploy. Ne pas commiter
le fichier rempli.

Vérifier en particulier :

```dotenv
COMPOSE_PROJECT_NAME=mevo2glpi-prod
APP_ENVIRONMENT=production
APP_RELEASE=vX.Y.Z
APP_COMMIT_SHA=SHA_GIT_COMPLET
DOKPLOY_NETWORK_NAME=dokploy-network
DATABASE_NETWORK_NAME=mevo2glpi-prod-database-network
TRANSCRIPTION_NETWORK_NAME=mevo2glpi-prod-transcription-network
AUDIO_VOLUME_NAME=mevo2glpi-prod-audio-data
```

Renseigner ensuite les valeurs préparées dans les étapes précédentes :

- `DATABASE_URL` et les chemins des fichiers hôte ;
- `WEB_AUTH_*` et `PUBLIC_BASE_URL` ;
- `OPENAI_COMPATIBLE_TRANSCRIPTION_*` ;
- `PUBLISHER_ENTRA_*` et `PUBLISHER_MAILBOX_ID` ;
- `GLPI_*`, le type de ticket et l'identifiant local de la source `MEVO` ;
- les délais et politiques de rétention validés.

Les valeurs recommandées de retry, timeout et rétention figurent dans le
fichier d'exemple. Ne les modifier qu'avec une justification d'exploitation.

## Domaine public

Créer un domaine uniquement pour le service `web` :

- nom : FQDN public préparé ;
- port interne : `8000` ;
- protocole applicatif : HTTP derrière Traefik ;
- exposition : HTTPS avec le certificat importé ;
- redirection HTTP vers HTTPS si le port 80 est autorisé.

`PUBLIC_BASE_URL` et `WEB_AUTH_REDIRECT_URI` doivent reprendre exactement ce
FQDN HTTPS. Ne créer aucun domaine pour PostgreSQL, les migrations, les workers
ou la transcription.

## Vérification avant déploiement

- [ ] le projet applicatif et, le cas échéant, le projet transcription pointent
  vers le même tag et le même SHA ;
- [ ] les noms de réseaux et de volume correspondent aux ressources créées ;
- [ ] tous les placeholders `CHANGE_ME` ont été remplacés ;
- [ ] les secrets sont saisis dans Dokploy et absents de Git ;
- [ ] les bind mounts hôte existent avant le déploiement ;
- [ ] seul le web possède un domaine ;
- [ ] l'auto-déploiement est désactivé.

Étape suivante : [déploiement](09-deploiement.md).
