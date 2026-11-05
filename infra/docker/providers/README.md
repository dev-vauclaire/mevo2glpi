# Providers de test

Ce dossier contient les images Docker réutilisables des services externes de
test et leurs recettes de déploiement en staging.

La cible de staging déploie séparément deux applications Dokploy de type
Dockerfile :

- [Microsoft Graph Simulator](microsoft-graph-simulator/README.md), qui
  reproduit le sous-ensemble de Microsoft Graph utilisé par le publisher ;
- [Transcribe AI Simulator](transcribe-ai-simulator/README.md), qui reproduit
  le contrat historique avec polling.

Le Compose `docker-compose.staging.yml` regroupant ces deux services reste
temporairement disponible comme mécanisme de retour arrière. Il sera supprimé
après validation des deux applications Dockerfile dans Dokploy ; il ne doit pas
être utilisé simultanément avec elles.

GLPI de test et le simulateur OpenAI-compatible sont utilisés par le Compose de
développement `../development/docker-compose.test-services.yml`. Le véritable
provider OpenAI-compatible Parakeet reste indépendant dans `../transcription/`.
Le lancement local, les ports loopback, le bootstrap GLPI et l'injection de
messages sont décrits dans la
[procédure de développement](../../../docs/procedures/developpement-local.md).

## Staging

Chaque application possède son propre `.env.example` et son propre README. Les
deux applications :

- construisent les deux simulateurs depuis le workspace uv racine ;
- rejoignent le réseau externe privé `gta-staging-simulators-network` avec un
  alias DNS stable ;
- n'exposent aucun port vers l'hôte ou Traefik ;
- exigent des clés exclusivement réservées au staging ;
- utilisent des services non privilégiés et des healthchecks bornés.

Microsoft Graph monte en plus les fixtures audio en lecture seule sans les
créer implicitement. L'interface Application documentée par Dokploy ne permet
pas de reproduire toutes les restrictions d'exécution du Compose. Cette limite
et les garanties réellement disponibles sont détaillées dans chaque README.

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
