# Providers de test

Ce dossier contient les images Docker réutilisables des services externes de
test et l'orchestration des simulateurs déployés en staging.

Le Compose `docker-compose.staging.yml` lance uniquement :

- `microsoft-graph-simulator`, qui reproduit le sous-ensemble de Microsoft
  Graph utilisé par le publisher ;
- `transcribe-ai-simulator`, qui reproduit le contrat historique avec polling.

GLPI de test et le simulateur OpenAI-compatible sont utilisés par le Compose de
développement `../development/docker-compose.test-services.yml`. Le véritable
provider OpenAI-compatible Parakeet reste indépendant dans `../transcription/`.
Le lancement local, les ports loopback, le bootstrap GLPI et l'injection de
messages sont décrits dans la
[procédure de développement](../../../docs/procedures/developpement-local.md).

## Staging

Le modèle de configuration est `staging.env.example`. La stack :

- construit les deux simulateurs depuis le workspace uv racine ;
- rejoint le réseau externe privé indiqué par `SIMULATORS_NETWORK_NAME` ;
- n'expose aucun port vers l'hôte ou Traefik ;
- exige des clés exclusivement réservées au staging ;
- monte les fixtures audio en lecture seule sans les créer implicitement ;
- utilise des services non privilégiés et des healthchecks bornés.

Les fixtures doivent être fictives, lisibles par l'UID/GID `10001` et ne
contenir aucune donnée réelle. La commande `reset` du simulateur Microsoft
Graph n'efface jamais ce répertoire.

Watch paths recommandés :

```text
providers/**
infra/docker/providers/**
.dockerignore
pyproject.toml
uv.lock
```

Les contrats HTTP et les commandes d'administration sont décrits avec les
simulateurs [Microsoft Graph](../../../providers/simulators/microsoft-graph-simulator/README.md),
[Transcribe AI](../../../providers/simulators/transcribe-ai-simulator/README.md)
et [OpenAI-compatible](../../../providers/simulators/openai-transcription-simulator/README.md).
