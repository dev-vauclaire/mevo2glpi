# Simulateurs de fournisseurs

Ce workspace uv est strictement réservé au staging. Il contient deux services
HTTP indépendants et sans donnée réelle :

- `microsoft-graph-simulator` reproduit le sous-ensemble de Microsoft Graph
  utilisé pour lire et classer les messages d'une boîte fictive ;
- `transcribe-ai-simulator` reproduit le contrat asynchrone de Transcribe AI.

Les deux états sont éphémères et disparaissent au redémarrage. Le simulateur
Microsoft ne reproduit pas Microsoft Entra ID : le futur publisher staging
utilise son mode d'authentification synthétique.

## Validation locale

```bash
uv sync --directory providers --locked --all-packages --all-groups
uv run --directory providers --all-packages pytest
uv run --directory providers ruff format --check .
uv run --directory providers ruff check .
```
