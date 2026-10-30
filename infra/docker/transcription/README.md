# Moteur de transcription Parakeet

Cette stack déploie le moteur ASR réel indépendamment des applications et des
simulateurs. Le même manifeste est destiné au staging puis, après recette, à
la production avec des réseaux, alias et secrets distincts.

Parakeet est accessible uniquement depuis `worker-automation` sur un réseau
Docker privé partagé. Le Compose ne publie aucun port vers l'hôte et ne relie
pas le service au réseau Traefik.

La stack épingle la variante FP32 : les essais en staging ont montré une
qualité de transcription française insuffisante avec la variante INT8. Le
conteneur doit disposer de 8 Gio de RAM. Ce choix ne modifie pas les formats
acceptés par le serveur : les WAV G.711 A-law ou μ-law restent incompatibles
tant qu'une normalisation PCM16 n'est pas ajoutée à la stack de transcription.

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
`PARAKEET_API_KEY`, puis déployer cette stack avant les applications staging.
La même clé doit être configurée dans le projet applicatif staging. Lors
d'une mise à niveau depuis la variante INT8, remplacer explicitement
`PARAKEET_MEMORY_LIMIT=3g` par `PARAKEET_MEMORY_LIMIT=8g` dans Dokploy avant le
redéploiement.

Watch path recommandé :

```text
infra/docker/transcription/**
```

## Vérifications

Le service `parakeet` doit devenir `healthy`. Depuis le terminal du conteneur
`worker-automation`, vérifier uniquement sa sonde, sans afficher de clé ou de
transcription :

```bash
python -c 'import httpx; print(httpx.get("http://gta-staging-parakeet:5092/health", timeout=5).json())'
```

Le résultat attendu contient `{"status":"ok"}`. Une recette avec des audios
synthétiques puis représentatifs reste nécessaire pour valider l'inférence,
les formats acceptés et les limites CPU/mémoire.

## Promotion future en production

La production reste pour l'instant sur `legacy_polling`. Après validation du
staging, le même Compose et le même digest pourront être déployés dans un
projet Dokploy de production, avec par exemple :

```text
COMPOSE_PROJECT_NAME=gta-prod-transcription
TRANSCRIPTION_NETWORK_NAME=gta-prod-transcription-network
PARAKEET_NETWORK_ALIAS=gta-prod-parakeet
```

La bascule applicative vers `openai_compatible` fera l'objet d'une itération
distincte. Elle ne doit pas réutiliser la clé API du staging.
