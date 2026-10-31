# API de transcription CPU

Cette stack déploie l'API OpenAI-compatible du projet avec Parakeet Ultra Q8
et `transcribe.cpp`. Elle reste indépendante des applications et des
simulateurs pendant la qualification staging. Après validation, elle pourra
être intégrée aux services applicatifs communs.

Le service est accessible uniquement par `worker-automation` sur un réseau
Docker privé partagé. Aucun port n'est publié vers l'hôte et le service n'est
pas relié au réseau Traefik.

L'image :

- charge Parakeet Ultra Q8 une seule fois au démarrage ;
- vérifie le SHA-256 du GGUF pendant le build ;
- normalise PCM, G.711 A-law, G.711 mu-law et OGG avec FFmpeg ;
- ne télécharge rien au runtime ;
- s'exécute sans privilège avec un système de fichiers en lecture seule.

Le modèle Q8 est la valeur par défaut. Un autre artefact approuvé peut être
testé en reconstruisant l'image avec une URL immuable et son SHA-256. Une URL
de modèle arbitraire n'est jamais acceptée au démarrage et
`TRANSCRIPTION_MODEL_PATH` ne déclenche aucun téléchargement.

## Déploiement staging

Créer une fois le réseau privé :

```bash
sudo docker network create --driver bridge --internal gta-staging-transcription-network
```

Créer un projet Docker Compose Dokploy utilisant :

```text
./infra/docker/transcription/docker-compose.transcription.yml
```

Copier les variables de `transcription.env.example`, remplacer
`APP_COMMIT_SHA` et `TRANSCRIPTION_API_KEY`, puis déployer cette stack avant
les applications staging. La même clé doit être configurée dans le projet
applicatif staging. `APP_RELEASE` et `APP_COMMIT_SHA` identifient exactement le
code ayant construit l'image.

Watch path recommandé :

```text
apps/transcription-api/**
infra/docker/transcription/**
pyproject.toml
uv.lock
```

## Vérifications

Le service `transcription-api` doit devenir `healthy`. Depuis le terminal du
conteneur `worker-automation`, vérifier sa sonde sans afficher de clé ni de
transcription :

```bash
python -c 'import httpx; print(httpx.get("http://gta-staging-transcription:5092/health/ready", timeout=5).json())'
```

Le résultat attendu indique que le modèle est prêt. Une recette avec des
audios synthétiques puis des enregistrements fictifs représentatifs reste
obligatoire pour valider la détection du français, la qualité et les limites
CPU/mémoire.

Le simulateur Microsoft Graph fournit notamment ces scénarios sans donnée
sensible :

```bash
glpi-ticketing-microsoft-graph-simulator-admin seed --scenario valid-wav
glpi-ticketing-microsoft-graph-simulator-admin seed --scenario valid-ogg
glpi-ticketing-microsoft-graph-simulator-admin seed --scenario valid-wav-alaw
glpi-ticketing-microsoft-graph-simulator-admin seed --scenario valid-wav-mulaw
```

Les erreurs de décodage sont terminales pour la tentative de transcription et
ne doivent jamais provoquer une boucle infinie. Les erreurs HTTP temporaires
restent soumises à la politique bornée de retry du worker.

## Critères de promotion

La production ne doit pas utiliser cette stack avant validation des points
suivants :

- français correctement détecté sur les messages courts ou bruités ;
- aucune perte de parole sur les messages de plus de 30 secondes présentant
  de fortes variations de volume ;
- latence, mémoire et démarrage acceptables sur le CPU cible ;
- PCM16, G.711 A-law, G.711 mu-law et OGG traités de bout en bout.

Le contrôle des variations de volume couvre notamment le
[problème long-form connu de transcribe.cpp 0.4.1](https://github.com/handy-computer/transcribe.cpp/issues/235).

Pour une future stack de production, utiliser au minimum des noms distincts :

```text
COMPOSE_PROJECT_NAME=gta-prod-transcription
TRANSCRIPTION_NETWORK_NAME=gta-prod-transcription-network
TRANSCRIPTION_NETWORK_ALIAS=gta-prod-transcription
```

La clé de staging ne doit jamais être réutilisée en production.
