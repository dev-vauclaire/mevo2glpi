# GLPI Ticketing Automation

POC Python destiné à automatiser la création de tickets GLPI à partir de
fichiers audio.

Le dépôt est organisé en workspace `uv` autour de trois membres :

- `glpi-ticketing-web` reçoit et restitue les audios ;
- `glpi-ticketing-worker-automation` porte le futur pipeline d'automatisation ;
- `glpi-ticketing-common` contient les primitives audio utilisées par les deux
  applications.

Le workspace partage un seul fichier `uv.lock`. Chaque application conserve
cependant son propre manifeste et doit déclarer explicitement ses dépendances.

## Prérequis

- Python 3.12 ou supérieur ;
- [`uv`](https://docs.astral.sh/uv/).

## Installation

```bash
cp .env.example .env
uv sync --all-packages --all-groups
```

Adaptez ensuite les valeurs de `.env` à votre environnement local. Ce fichier
est ignoré par Git et ne doit contenir que vos valeurs locales.

## Vérifications

```bash
uv run --all-packages pytest -m "not glpi and not live_transcribe and not e2e"
uv run ruff format --check .
uv run ruff check .
```

La suite d'un membre peut être ciblée par son chemin tout en conservant les
outils de développement déclarés à la racine :

```bash
uv run --all-packages pytest apps/web/tests
```
