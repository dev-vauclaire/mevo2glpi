# 7. Choisir la transcription

Le worker consomme exclusivement une API synchrone compatible avec le contrat
OpenAI `POST /v1/audio/transcriptions`. Choisir une seule des deux variantes.

## Variante A — Parakeet local dans Dokploy

Créer un projet Compose distinct à partir de :

```text
infra/docker/transcription/docker-compose.transcription.yml
```

Copier les valeurs de référence de
[`production.env.example`](../../../infra/docker/transcription/production.env.example)
dans l'environnement Dokploy, puis remplacer uniquement les placeholders :

```dotenv
COMPOSE_PROJECT_NAME=mevo2glpi-prod-transcription
APP_RELEASE=vX.Y.Z
APP_COMMIT_SHA=SHA_GIT_COMPLET
TRANSCRIPTION_NETWORK_NAME=mevo2glpi-prod-transcription-network
TRANSCRIPTION_NETWORK_ALIAS=mevo2glpi-prod-transcription
TRANSCRIPTION_API_KEY=SECRET_ALEATOIRE
```

Conserver l'URL et le SHA-256 épinglés du modèle, sauf qualification explicite
d'un autre artefact. Le profil de référence réserve 2 CPU, 4 Gio et deux
threads. Le service reste privé et expose uniquement le port `5092` au réseau
Docker.

Dans le projet applicatif :

```dotenv
TRANSCRIPTION_PROVIDER=openai_compatible
OPENAI_COMPATIBLE_TRANSCRIPTION_BASE_URL=http://mevo2glpi-prod-transcription:5092/v1
OPENAI_COMPATIBLE_TRANSCRIPTION_API_KEY=MEME_SECRET
OPENAI_COMPATIBLE_TRANSCRIPTION_MODEL=parakeet-ultra
OPENAI_COMPATIBLE_TRANSCRIPTION_LANGUAGE=fr
TRANSCRIPTION_HTTP_TIMEOUT_SECONDS=180
```

La readiness `/health/ready` ne réussit qu'après le chargement du modèle.

## Variante B — Provider externe

Ne pas créer le projet Parakeet. Configurer :

```dotenv
TRANSCRIPTION_PROVIDER=openai_compatible
OPENAI_COMPATIBLE_TRANSCRIPTION_BASE_URL=https://NOM_PROVIDER/v1
OPENAI_COMPATIBLE_TRANSCRIPTION_API_KEY=SECRET_DOKPLOY
OPENAI_COMPATIBLE_TRANSCRIPTION_MODEL=MODELE_QUALIFIE
OPENAI_COMPATIBLE_TRANSCRIPTION_LANGUAGE=fr
TRANSCRIPTION_HTTP_TIMEOUT_SECONDS=180
```

Qualifier le contrat, les limites de taille, les formats audio, le timeout, la
confidentialité et la chaîne TLS du fournisseur. Le réseau de transcription
déclaré par le Compose applicatif reste requis comme ressource Docker, même si
aucun service local n'y est attaché ; les sorties vers le provider passent par
le réseau de base de données non `internal`.

## Règles communes

- la clé n'est jamais journalisée ;
- le contenu de la transcription n'est pas journalisé ;
- une indisponibilité temporaire déclenche le retry borné du worker ;
- une erreur terminale de transcription produit un ticket dégradé au lieu de
  bloquer indéfiniment le pipeline ;
- la limite nominale d'upload est de 25 Mio et la durée locale de 600 secondes.

## Porte de validation

- [ ] une seule variante est retenue ;
- [ ] le modèle et le contrat sont qualifiés ;
- [ ] les clés du provider et de l'application concordent ;
- [ ] aucun port de transcription local n'est publié ;
- [ ] la readiness locale ou l'endpoint externe répond depuis le worker ;
- [ ] le timeout couvre la durée maximale attendue sans multiplier les appels.

Étape suivante : [configuration Dokploy](08-configuration-dokploy.md).
