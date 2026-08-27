# Pipeline E2E du worker

Cette suite assemble le vrai `worker_runtime()` avec les deux
stacks Testcontainers existantes. Elle soumet l'audio vocal synthétique de la
fixture Transcribe AI, attend sa transcription, crée un ticket dans GLPI et
vérifie la publication atomique de l'audio dans `completed`.

Les identifiants Transcribe AI, MySQL, utilisateur GLPI et OAuth sont générés à
chaque session. Les fixtures propres à chaque service restent responsables de
la disponibilité et du nettoyage des conteneurs, réseaux et volumes. Le test
E2E ne duplique donc aucun bootstrap.

Le test accepte les outcomes `retry` dans une fenêtre bornée, puis exige un
`CompletedOutcome`. Il relit le ticket par `external_id` et vérifie une
transcription non vide, l'identifiant du ticket et l'URL audio. Il confirme
ensuite que `pending` et `processing` sont vides, que l'audio est intact dans
`completed` et qu'aucun artefact n'existe dans `failed`.

Le mode CPU est utilisé par défaut :

```bash
uv run pytest -m e2e apps/worker-automation/tests/integration/e2e -vv --maxfail=1
```

Le mode GPU réutilise l'overlay Transcribe AI existant :

```bash
TRANSCRIBE_AI_TEST_DEVICE=gpu \
uv run pytest -m e2e apps/worker-automation/tests/integration/e2e -vv --maxfail=1
```

Cette suite ne doit pas être exécutée avec `pytest-xdist`. Elle nécessite
Docker, le plugin Compose, une architecture `linux/amd64` et un accès réseau au
premier lancement pour les images et le modèle de transcription.
