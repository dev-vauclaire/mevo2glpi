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

Créer aussi le répertoire hôte destiné aux seuls enregistrements de recette.
Le groupe `10001` correspond au groupe non privilégié du simulateur :

```bash
sudo install -d -o root -g 10001 -m 0750 /srv/gta-staging/audio-fixtures
```

Configurer ensuite
`MICROSOFT_GRAPH_SIMULATOR_AUDIO_FIXTURES_HOST_PATH=/srv/gta-staging/audio-fixtures`
dans Dokploy. Le Compose monte ce répertoire dans `/fixtures/audio` en lecture
seule et refuse de le créer implicitement s'il est absent.

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

Pour tester un enregistrement WAV ou OGG fictif créé par l'opérateur, l'installer
dans le répertoire préparé sur la VM avec des droits minimaux :

```bash
sudo install -o root -g 10001 -m 0640 ./test-vocal.wav /srv/gta-staging/audio-fixtures/test-vocal.wav
```

Depuis le terminal Dokploy du simulateur Microsoft, injecter ensuite le fichier
monté en lecture seule :

```bash
glpi-ticketing-microsoft-graph-simulator-admin seed --audio-path /fixtures/audio/test-vocal.wav
```

La commande `reset` efface uniquement la boîte simulée en mémoire : elle ne
supprime jamais le fichier source sur la VM. Retirer manuellement ce dernier à
la fin de la recette. Il ne doit jamais être ajouté au dépôt. Ne pas utiliser de
donnée patient, de message vocal de production ou d'autre donnée sensible.

Depuis le terminal du simulateur Transcribe AI :

```bash
glpi-ticketing-transcribe-ai-simulator-smoke
```

Les scénarios utilisent uniquement des données synthétiques. Aucun secret ou
audio de production ne doit être ajouté au dépôt ou à cette stack.
