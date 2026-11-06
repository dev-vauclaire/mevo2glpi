# API de transcription CPU

Cette stack déploie l'API OpenAI-compatible du projet avec Parakeet Ultra Q8
et `transcribe.cpp`. Depuis `v0.4.0`, elle reste volontairement indépendante
du Compose applicatif et des simulateurs dans tous les environnements. Cette
séparation permet de qualifier, dimensionner, redémarrer ou remplacer le
provider CPU sans redéployer les autres services.

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

## Contrat de la stack

Le point d'entrée est `docker-compose.transcription.yml`. Deux modèles de
configuration explicites évitent de mélanger les réseaux et identités :

- `staging.env.example` pour le staging ;
- `production.env.example` pour la production.

La stack exige :

- un réseau externe privé nommé par `TRANSCRIPTION_NETWORK_NAME` ;
- une `TRANSCRIPTION_API_KEY` partagée uniquement avec le worker
  d'automatisation du même environnement ;
- une release et un SHA Git complet injectés au build ;
- une URL immuable et un SHA-256 lorsque le modèle par défaut est remplacé ;
- des limites CPU, mémoire, taille et durée adaptées à l'environnement.

Le service rejoint ce réseau avec `TRANSCRIPTION_NETWORK_ALIAS`, expose
uniquement le port conteneur `5092` et ne rejoint jamais Traefik. Le projet
applicatif sélectionne `TRANSCRIPTION_PROVIDER=openai_compatible` et utilise
une URL de base terminée par `/v1`. Aucun basculement automatique avec
`legacy_polling` n'est réalisé.

Watch path recommandé :

```text
apps/transcription-api/**
infra/docker/transcription/**
pyproject.toml
uv.lock
```

## Healthcheck

Le service doit devenir `healthy`. Sa readiness interne est exposée sur
`GET /health/ready`. Depuis un conteneur autorisé du même réseau, elle peut être
interrogée sans afficher de clé ni de transcription :

```bash
python -c 'import httpx; print(httpx.get("http://mevo2glpi-staging-transcription:5092/health/ready", timeout=5).json())'
```

En production, utiliser l'alias `mevo2glpi-prod-transcription` à la place de l'alias
de staging.

Le résultat attendu indique que le modèle est prêt. Les erreurs de décodage
sont terminales pour la tentative de transcription et ne doivent jamais
provoquer une boucle infinie. Les erreurs HTTP temporaires restent soumises à
la politique bornée de retry du worker.

## Critères de validation d'une release

Une nouvelle version du moteur, du modèle ou de l'image ne doit pas être promue
avant validation des points suivants :

- français correctement détecté sur les messages courts ou bruités ;
- aucune perte de parole sur les messages de plus de 30 secondes présentant
  de fortes variations de volume ;
- latence, mémoire et démarrage acceptables sur le CPU cible ;
- PCM16, G.711 A-law, G.711 mu-law et OGG traités de bout en bout.

Le contrôle des variations de volume couvre notamment le
[problème long-form connu de transcribe.cpp 0.4.1](https://github.com/handy-computer/transcribe.cpp/issues/235).

L'ordre de déploiement, les valeurs propres à chaque environnement et la
recette audio sont décrits dans les procédures de
[staging](../../../docs/procedures/staging.md) et de
[production](../../../docs/procedures/production.md). Les réseaux, clés et
noms de projet doivent être distincts entre ces environnements.
