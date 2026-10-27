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
- `glpi-db` fournit le MySQL privé de GLPI.
- `glpi-staging` exécute le véritable GLPI 11.0.7 de recette.
- `glpi-bootstrap` active l'API V2 et réconcilie les identités et le client
  OAuth de staging.

## Réseaux et volumes

Créer une fois le réseau partagé avant le premier déploiement :

```bash
sudo docker network create --driver bridge --internal gta-staging-providers-network
```

La stack utilise également :

- `glpi-database`, réseau privé créé par Compose ;
- `dokploy-network`, réseau externe utilisé uniquement par `glpi-staging` pour
  Traefik ;
- `gta-staging-providers-glpi-data`, volume neuf des fichiers GLPI ;
- `gta-staging-providers-glpi-database-data`, volume neuf de MySQL.

Ne jamais lancer `docker compose down --volumes` sur cette stack sans décision
explicite de supprimer toutes les données de recette.

## Déploiement Dokploy

Créer un projet Docker Compose depuis la branche `feat/staging-providers` avec
le chemin suivant :

```text
./infra/docker/providers/docker-compose.providers.yml
```

Copier ensuite toutes les variables de `providers.env.example` dans l'onglet
Environment de Dokploy et remplacer chaque valeur `CHANGE_ME`. Aucun secret ne
doit être commité. Conserver ensuite ces valeurs entre les redéploiements : le
bootstrap GLPI refuse volontairement une rotation implicite de
`GLPI_PASSWORD`, `GLPI_ADMIN_PASSWORD`, `GLPI_CLIENT_ID` et
`GLPI_CLIENT_SECRET`. Une régénération exige donc une procédure de rotation
future ou, pour ce jalon à données jetables, une décision explicite de recréer
les deux volumes GLPI.

Configurer les watch paths suivants si le projet Dokploy doit se redéployer
sur les seuls changements de cette stack :

```text
providers/**
infra/docker/providers/**
.dockerignore
pyproject.toml
uv.lock
```

Arrêter l'ancienne stack staging avant d'associer au nouveau service un domaine
qu'elle utilise déjà. Deux projets Dokploy ne doivent pas publier simultanément
le même domaine GLPI.

Déployer une première fois, puis associer le domaine GLPI au service
`glpi-staging` sur le port interne `80` et redéployer la stack. Aucun domaine ne
doit être créé pour les simulateurs, MySQL ou le bootstrap.

## Vérifications

Les états attendus sont :

- `microsoft-graph-simulator` : `healthy` ;
- `transcribe-ai-simulator` : `healthy` ;
- `glpi-db` : `healthy` ;
- `glpi-bootstrap` : terminé avec le code `0` ;
- `glpi-staging` : `healthy` et accessible par son domaine de staging exposé
  par Traefik.

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
