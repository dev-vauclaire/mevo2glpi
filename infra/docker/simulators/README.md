# Simulateurs de staging

Cette stack est strictement réservée au staging. Elle contient uniquement des
doubles de test sans donnée réelle :

- `microsoft-graph-simulator` reproduit le sous-ensemble de Microsoft Graph
  utilisé par le publisher ;
- `transcribe-ai-simulator` conserve le contrat du provider de transcription
  historique pour les tests contractuels et le repli staging.

Parakeet n'appartient pas à cette stack : le moteur réel est déployé avec le
Compose autonome décrit dans
[`../transcription/README.md`](../transcription/README.md).

La simulation GLPI 11.0.7 reste versionnée mais désactivée. Sa réactivation
nécessite de décommenter ses trois services, ses réseaux, ses volumes et les
variables associées dans `simulators.env.example`.

## Déploiement Dokploy

Créer une fois le réseau Docker privé partagé par les simulateurs et les
applications staging :

```bash
sudo docker network create --driver bridge --internal gta-staging-simulators-network
```

Créer ensuite un projet Docker Compose Dokploy utilisant :

```text
./infra/docker/simulators/docker-compose.simulators.yml
```

Copier les variables de `simulators.env.example` dans l'environnement Dokploy,
remplacer toutes les valeurs `CHANGE_ME`, puis vérifier que les deux services
sont `healthy`. Aucun port ne doit être publié vers l'hôte ou Traefik.

Watch paths recommandés :

```text
providers/**
infra/docker/simulators/**
.dockerignore
pyproject.toml
uv.lock
```

## Vérifications

Depuis le terminal du simulateur Microsoft :

```bash
glpi-ticketing-microsoft-graph-simulator-admin list-scenarios
glpi-ticketing-microsoft-graph-simulator-admin seed --scenario valid-wav
glpi-ticketing-microsoft-graph-simulator-admin seed --scenario valid-ogg
glpi-ticketing-microsoft-graph-simulator-admin reset
```

Pour tester un enregistrement WAV ou OGG fictif créé par l'opérateur, le copier
temporairement depuis la VM dans le `/tmp` du conteneur, puis le référencer :

```bash
sudo docker ps --filter name=microsoft-graph-simulator --format '{{.ID}} {{.Names}}'
sudo docker cp ./test-vocal.wav <CONTAINER_ID>:/tmp/test-vocal.wav
sudo docker exec <CONTAINER_ID> glpi-ticketing-microsoft-graph-simulator-admin seed --audio-path /tmp/test-vocal.wav
```

Le fichier disparaît avec le conteneur et n'est jamais ajouté au dépôt. Ne pas
utiliser de donnée patient ou de message vocal de production.

Depuis le terminal du simulateur Transcribe AI :

```bash
glpi-ticketing-transcribe-ai-simulator-smoke
```

Les scénarios utilisent uniquement des données synthétiques. Aucun secret ou
audio de production ne doit être ajouté au dépôt ou à cette stack.
