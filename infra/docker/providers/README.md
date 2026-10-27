# Providers de staging

Cette stack est strictement réservée aux recettes synthétiques. Elle ne doit
jamais être raccordée à une boîte Exchange, un audio ou une base GLPI de
production.

## Services

- `microsoft-graph-simulator` simule Microsoft Graph et une boîte dédiée. Il ne
  simule pas Microsoft Entra ID ; l'authentification du futur publisher staging
  restera synthétique.
- `transcribe-ai-simulator` simule la création et le polling d'un job de
  transcription.

La simulation GLPI 11.0.7 reste versionnée mais désactivée. Pour l'utiliser,
décommenter `glpi-db`, `glpi-staging`, `glpi-bootstrap`, leurs réseaux et
volumes, puis les variables GLPI de `providers.env.example`.

## Réseaux et volumes

Créer une fois le réseau partagé avant le premier déploiement :

```bash
sudo docker network create --driver bridge --internal gta-staging-providers-network
```

## Déploiement Dokploy

Créer un projet Docker Compose depuis la branche `feat/staging-providers` avec
le chemin suivant :

```text
./infra/docker/providers/docker-compose.providers.yml
```

Copier les variables actives de `providers.env.example` dans l'onglet
Environment de Dokploy et remplacer chaque valeur `CHANGE_ME`. Aucun secret ne
doit être commité.

Configurer les watch paths suivants si le projet Dokploy doit se redéployer
sur les seuls changements de cette stack :

```text
providers/**
infra/docker/providers/**
.dockerignore
pyproject.toml
uv.lock
```

## Vérifications

Les états attendus sont :

- `microsoft-graph-simulator` : `healthy` ;
- `transcribe-ai-simulator` : `healthy`.

Depuis le terminal Dokploy du simulateur Microsoft, une recette synthétique
peut être injectée puis effacée :

```bash
glpi-ticketing-microsoft-graph-simulator-admin seed --extension wav
glpi-ticketing-microsoft-graph-simulator-admin seed --extension ogg
glpi-ticketing-microsoft-graph-simulator-admin reset
```

Depuis le terminal du simulateur Transcribe AI, la commande suivante vérifie
la création du job puis les états `PENDING` et `COMPLETED`, sans afficher la
transcription :

```bash
glpi-ticketing-transcribe-ai-simulator-smoke
```
