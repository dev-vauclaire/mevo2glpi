# Référence des tests

Ce document décrit la stratégie de validation transversale du dépôt et les
commandes à exécuter depuis sa racine. Les README placés avec chaque application
ou package restent la référence pour leurs scénarios ciblés.

Les tests utilisent exclusivement des identifiants, messages et audios
synthétiques. Aucun secret, certificat privé, jeton, fichier d'environnement ou
contenu réel de production ne doit être utilisé.

## Niveaux de validation

| Niveau | Marqueur Pytest | Périmètre | Dépendance principale |
| --- | --- | --- | --- |
| Unitaire et contractuel déterministe | aucun marqueur spécialisé | Règles métier, parseurs, clients simulés, API et boucles de workers | Aucune infrastructure externe |
| Persistance | `database` | Migrations, contraintes, repositories, transactions et verrous | PostgreSQL Testcontainers |
| Déploiement | `deployment` | Dockerfiles, rendu Compose, réseaux, volumes, sécurité et healthchecks | Docker et Docker Compose |
| Contrat GLPI | `glpi` | OAuth, API GLPI V2.3.0 et création de ticket | GLPI 11.0.7 et MySQL jetables |
| Provider historique | `transcribe_ai` | Soumission, polling et réponses du provider historique | Stack Transcribe AI jetable |
| Modèle local réel | `transcription_model` | Chargement et inférence du GGUF Parakeet Ultra | Modèle fourni localement et CPU |
| Parcours aval complet | `e2e` | Migration, automation, GLPI, web et rétention | PostgreSQL, GLPI et provider de transcription jetables |
| Parcours nominal Parakeet | `e2e` et `transcription_model` | Audio synthétique, transcription CPU réelle et ticket GLPI corrélé | Image Parakeet épinglée, modèle GGUF, PostgreSQL et GLPI jetables |

Les marqueurs sont déclarés dans le [`pyproject.toml`](../../pyproject.toml).

## Préparation

Prérequis communs :

- Python 3.12 et `uv` ;
- Docker Engine et Docker Compose pour les suites conteneurisées ;
- des ports loopback éphémères disponibles ;
- suffisamment de mémoire et d'espace disque pour les images et modèles ;
- aucune autre suite Docker lourde de ce dépôt exécutée en parallèle.

Synchroniser l'environnement verrouillé :

```bash
uv sync --locked --all-packages --all-groups
```

## Qualité et suite déterministe

```bash
uv run ruff format --check .
uv run ruff check .
uv run --all-packages pytest apps packages tests \
  -m "not database and not deployment and not glpi and not transcription_model and not transcribe_ai and not e2e" \
  --maxfail=1
uv run pytest providers --maxfail=1
```

Les workflows `ci-apps.yml` et `ci-providers.yml` exécutent ces contrôles selon
les chemins modifiés. La CI des applications valide également PostgreSQL, les
migrations et les principaux contrats statiques des fichiers Compose.

## Suites spécialisées

### PostgreSQL et migrations

```bash
uv run --all-packages pytest -m database -vv --maxfail=1
```

La suite couvre une base neuve et les chemins de mise à niveau explicitement
pris en charge. Les fixtures utilisent une base isolée et détruisent les
ressources qu'elles ont créées.

### Contrats de déploiement

```bash
uv run pytest -m deployment tests/deployment providers/tests/deployment \
  -vv --maxfail=1
```

Ces tests valident les manifests et les contrats Docker. Ils ne constituent pas
un déploiement Dokploy et ne contactent aucun service réel.

### GLPI

```bash
uv run --all-packages pytest -m glpi -vv --maxfail=1
```

La stack GLPI est jetable. Cette commande couvre le client partagé et
l'intégration du worker de rétention. Elle ne doit jamais être remplacée dans
cette suite par une instance de staging ou de production.

### Provider Transcribe AI historique

Le passage CPU est la référence :

```bash
TRANSCRIBE_AI_TEST_DEVICE=cpu \
uv run --all-packages pytest -m transcribe_ai -vv --maxfail=1
```

Une validation GPU peut compléter la preuve lorsqu'un runtime NVIDIA est
disponible, sans remplacer le passage CPU :

```bash
TRANSCRIBE_AI_TEST_DEVICE=gpu \
uv run --all-packages pytest -m transcribe_ai -vv --maxfail=1
```

### Modèle Parakeet Ultra réel

Les tests ordinaires de `transcription-api` injectent un moteur et ne chargent
pas le modèle. Le smoke test réel exige un GGUF fourni explicitement :

```bash
TRANSCRIPTION_MODEL_TEST_PATH=/chemin/vers/parakeet-ultra-Q8_0.gguf \
uv run pytest -m transcription_model \
  apps/transcription-api/tests/integration/test_real_model.py -vv --maxfail=1
```

Ce test vérifie le chargement et l'inférence, pas la qualité fonctionnelle. La
qualité du français doit être qualifiée en staging avec des audios fictifs
représentatifs.

### Parcours E2E aval

Le parcours nominal construit l'API CPU Parakeet épinglée, injecte un audio
français synthétique, attend la fin du job puis vérifie les événements de
transcription et le ticket GLPI corrélé :

```bash
uv run pytest -m "e2e and transcription_model" \
  tests/e2e/test_parakeet_nominal.py -vv --maxfail=1
```

Le premier build télécharge les couches et le modèle GGUF épinglé, puis
construit l'image locale. Il exige donc un accès aux sources configurées, du
temps, de l'espace disque et suffisamment de mémoire. L'inférence reste
strictement exécutée sur CPU.

La régression du provider historique reste disponible séparément. Son passage
CPU est séquentiel :

```bash
TRANSCRIBE_AI_TEST_DEVICE=cpu \
uv run --all-packages pytest -m "e2e and not transcription_model" \
  apps/worker-automation/tests/integration/e2e tests/e2e \
  -vv --maxfail=1
```

Son mode GPU est optionnel :

```bash
TRANSCRIBE_AI_TEST_DEVICE=gpu \
uv run --all-packages pytest -m "e2e and not transcription_model" \
  apps/worker-automation/tests/integration/e2e tests/e2e \
  -vv --maxfail=1
```

L'E2E démarre la migration, le web, le worker d'automatisation et le worker de
rétention. Il reproduit la frontière durable du publisher avec un helper
réservé aux tests, mais ne contacte pas Microsoft 365 et ne démarre pas le
publisher réel. Les contrats Microsoft Graph et l'orchestration du publisher
sont couverts séparément par ses tests et le simulateur stateful.

Le scénario nominal qualifie le contrat OpenAI-compatible et le modèle
Parakeet Ultra sur un audio synthétique ; les autres scénarios de résilience
utilisent encore le provider historique jetable. Aucun de ces parcours ne
qualifie Traefik, les certificats TLS ou un tenant Entra ID réel. La topologie
et les scénarios sont détaillés dans le
[README de la suite E2E](../../tests/e2e/README.md).

## Séquentialité et nettoyage

Les suites `database`, `deployment`, `glpi`, `transcribe_ai` et `e2e` ne doivent
pas être parallélisées entre elles. N'utilisez ni `pytest-xdist`, ni l'option
`-n` pour les suites qui partagent le daemon Docker.

Chaque fixture ne supprime que les conteneurs, réseaux et volumes qu'elle a
créés. Une erreur de disponibilité doit rester bornée, déclencher ce nettoyage
et produire un diagnostic expurgé. Ne lancez jamais de nettoyage Docker global
pour terminer une suite de ce dépôt.

## Frontières avec la recette opérateur

Les tests automatisés prouvent les contrats logiciels dans un environnement
jetable. La procédure de staging complète cette preuve avec :

- Microsoft Graph ou son simulateur selon le mode retenu ;
- le provider de transcription réellement sélectionné ;
- une instance GLPI de test ;
- le routage Dokploy et les certificats internes ;
- une validation fonctionnelle sur des audios fictifs représentatifs.

Une panne ne doit jamais être injectée dans Exchange, GLPI ou un provider de
production pour valider un scénario de test.
