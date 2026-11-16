# Étape 7 — Choisir la transcription

## Objectif

Configurer le contrat synchrone OpenAI-compatible attendu par automation. La
transcription peut rester arrêtée pour le dashboard, le publisher ou un test
qui ne lance pas automation.

## Option A — Simulateur OpenAI-compatible

Démarrer le double HTTP déterministe :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml up --detach --build --wait openai-transcription-simulator
```

Configurer `.env` :

```dotenv
TRANSCRIPTION_PROVIDER=openai_compatible
OPENAI_COMPATIBLE_TRANSCRIPTION_BASE_URL=http://127.0.0.1:18082/v1
OPENAI_COMPATIBLE_TRANSCRIPTION_API_KEY=synthetic-development-openai-key
OPENAI_COMPATIBLE_TRANSCRIPTION_MODEL=synthetic-openai-model
OPENAI_COMPATIBLE_TRANSCRIPTION_LANGUAGE=fr
```

Vérification :

```bash
curl --fail http://127.0.0.1:18082/health
```

Le simulateur valide le contrat et les erreurs, mais pas la qualité de la
transcription.

## Option B — Parakeet local

La stack CPU réelle est décrite dans le
[README de la transcription](../../../infra/docker/transcription/README.md).
Elle nécessite un réseau Docker privé et ne publie aucun port vers l'hôte dans
sa recette Dokploy. Pour un test local, utiliser sa procédure dédiée ou une
instance déjà disponible, puis configurer son URL `/v1`, sa clé et le modèle
`parakeet-ultra` dans `.env`.

Avant de lancer automation, vérifier :

- la readiness du service ;
- les ressources CPU et mémoire disponibles ;
- le timeout HTTP adapté à la durée maximale des audios ;
- la prise en charge du format audio testé.

## Option C — Aucun provider

Ne pas lancer `worker-automation`. Le web, les migrations et le publisher
peuvent être développés séparément, mais aucun job ne pourra atteindre la
création du ticket.

## Résultat attendu

Lorsque automation est lancé, l'URL configurée répond au contrat
`POST /v1/audio/transcriptions`. Un échec technique reste soumis aux retries
bornés ; une erreur métier terminale ne doit pas être masquée par un autre
provider.

## Étape suivante

[Lancer les applications](08-applications.md) nécessaires au profil choisi.
