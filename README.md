# GLPI Ticketing Automation

POC Python destiné à automatiser la création de tickets GLPI à partir de
fichiers audio. Le dépôt contient pour le moment le socle du projet et sa
configuration typée ; aucun service externe n'est encore appelé.

## Prérequis

- Python 3.12 ou supérieur ;
- [`uv`](https://docs.astral.sh/uv/).

## Installation

```bash
cp .env.example .env
uv sync --all-groups
```

Adaptez ensuite les valeurs de `.env` à votre environnement local. Ce fichier
est ignoré par Git et ne doit contenir que vos valeurs locales.

## Vérifications

```bash
uv run pytest tests/unit/test_settings.py
uv run ruff format --check .
uv run ruff check .
```
