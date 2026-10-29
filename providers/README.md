# Simulateurs de fournisseurs

Ce dossier appartient au workspace uv racine et reste strictement réservé au
staging. Il contient deux services HTTP indépendants et sans donnée réelle :

- `microsoft-graph-simulator` reproduit le sous-ensemble de Microsoft Graph
  utilisé pour lire et classer les messages d'une boîte fictive ;
- `transcribe-ai-simulator` reproduit le contrat asynchrone de Transcribe AI.

Les deux états sont éphémères et disparaissent au redémarrage. Le simulateur
Microsoft ne reproduit pas Microsoft Entra ID : le publisher staging utilise
son mode d'authentification synthétique.

## Validation locale

La suite complète nécessite la commande `docker compose` pour rendre les
manifestes des simulateurs et du moteur de transcription, mais elle ne démarre
aucun conteneur et ne télécharge aucune image.

```bash
uv sync --locked --all-packages --all-groups
uv run pytest providers --maxfail=1
uv run ruff format --check providers
uv run ruff check providers
```
