# Simulateurs de staging

Le Compose [`docker-compose.staging.yml`](docker-compose.staging.yml) déploie
dans une même application Dokploy les deux contrats externes simulés :

- Microsoft Graph, utilisé par le publisher pour découvrir et télécharger les
  messages vocaux ;
- une API de transcription synchrone OpenAI-compatible.

GLPI de test reste une application Compose distincte dans `glpi-test/`. Le
provider CPU réel reste une stack distincte dans `../transcription/`. Le Compose
de développement complet se trouve dans
`../development/docker-compose.test-services.yml`.

## Déploiement

Le réseau externe privé `mevo2glpi-staging-simulators-network` doit exister
avant le déploiement. Dans Dokploy, les deux services rejoignent ce réseau et
restent inaccessibles depuis l'hôte et Traefik. Le Compose leur attribue des
alias DNS stables :

| Service | Alias interne |
| --- | --- |
| Microsoft Graph | `mevo2glpi-staging-microsoft-graph` |
| OpenAI-compatible | `mevo2glpi-staging-openai-transcription` |

Copier les variables de [`staging.env.example`](staging.env.example) dans
l'environnement Dokploy, puis remplacer toutes les valeurs `CHANGE_ME` par des
secrets synthétiques réservés au staging. Aucun secret réel ne doit être placé
dans le dépôt.

Le répertoire désigné par
`MICROSOFT_GRAPH_SIMULATOR_AUDIO_FIXTURES_HOST_PATH` doit exister avant le
déploiement. Il est monté en lecture seule, sans création implicite, et ne doit
contenir que des audios fictifs lisibles par l'UID/GID `10001`. La commande
`reset` du simulateur Microsoft Graph ne supprime jamais ces fichiers.

Validation locale du manifeste, sans démarrer de conteneur :

```bash
docker compose \
  --env-file infra/docker/providers/staging.env.example \
  --file infra/docker/providers/docker-compose.staging.yml \
  config --quiet
```

## Garanties d'exécution

Les deux services utilisent un utilisateur non privilégié, une racine en
lecture seule, un `tmpfs` borné, `cap_drop: ALL`, `no-new-privileges`, une limite
de processus, un healthcheck et une rotation des journaux. Seul Microsoft Graph
monte un répertoire hôte.

Les tests locaux créent un réseau `bridge` jetable afin de ne pas dépendre de
Swarm. Sur la VM Dokploy, le réseau externe doit conserver la topologie retenue
par la plateforme ; ce détail ne change ni les alias DNS ni le contrat du
Compose.

Watch paths recommandés :

```text
providers/**
infra/docker/providers/**
.dockerignore
pyproject.toml
uv.lock
```

Les contrats HTTP et les commandes d'administration sont décrits dans les
README des simulateurs [Microsoft Graph](../../../providers/simulators/microsoft-graph-simulator/README.md)
et [OpenAI-compatible](../../../providers/simulators/openai-transcription-simulator/README.md).
