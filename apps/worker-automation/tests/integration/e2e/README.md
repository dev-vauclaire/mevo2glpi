# Pipeline E2E persistant du worker

Cette suite assemble le vrai `worker_runtime()`, une base PostgreSQL migrée et
les stacks Testcontainers Transcribe AI et GLPI. Elle publie l'audio vocal
synthétique à sa clé canonique, crée le job `pending`, puis laisse le worker
piloter tout le cycle de vie depuis PostgreSQL.

Le scénario vérifie :

- le passage durable du job à `completed` ;
- l'historique `job_created` → `processing_started` → soumission et succès de
  transcription → ticket confirmé → job terminé ;
- la persistance de l'identifiant GLPI et la création de la rétention ;
- le ticket réel retrouvé par `external_id = audio_uuid` ;
- une transcription non vide et l'URL audio dans le contenu du ticket ;
- la présence d'un seul fichier inchangé à sa `storage_key` canonique.

Le test ne recherche aucun UUID par scan et n'attend aucun répertoire
`pending`, `processing`, `completed` ou `failed`. Les états sont des valeurs
PostgreSQL ; le worker ne déplace pas l'audio et ne produit aucun sidecar.

Les identifiants Transcribe AI, MySQL, utilisateur GLPI et OAuth sont générés à
chaque session. Les fixtures propres à chaque service et à PostgreSQL restent
responsables de la disponibilité et du nettoyage des conteneurs, réseaux,
volumes et bases de test.

Le test accepte les outcomes `retry` dans une fenêtre bornée, attend leur
échéance persistée, puis exige un `CompletedOutcome`. L'historique nominal est
vérifié comme une sous-séquence ordonnée : les événements de retry et de reprise
peuvent s'intercaler, mais aucun événement terminal `failed` ou `blocked` n'est
accepté.

Le mode CPU est utilisé par défaut :

```bash
uv run pytest -m e2e \
  apps/worker-automation/tests/integration/e2e -vv --maxfail=1
```

Le mode GPU réutilise l'overlay Transcribe AI existant :

```bash
TRANSCRIBE_AI_TEST_DEVICE=gpu \
uv run pytest -m e2e \
  apps/worker-automation/tests/integration/e2e -vv --maxfail=1
```

Cette suite ne doit pas être exécutée avec `pytest-xdist`. Elle nécessite
Docker, le plugin Compose, une architecture `linux/amd64` et un accès réseau au
premier lancement pour les images et le modèle de transcription.
