# Architecture du dépôt

## Workspace uv

La racine est un workspace uv non publiable. `uv.lock` est l'unique verrou de
dépendances pour ses douze membres. Le tableau ci-dessous reprend uniquement les
chemins déclarés dans `[tool.uv.workspace].members` du fichier
[`pyproject.toml`](../../pyproject.toml).

| Membre | Responsabilité | Documentation |
| --- | --- | --- |
| `apps/database-migrations` | Migration Alembic one-shot | [README](../../apps/database-migrations/README.md) |
| `apps/transcription-api` | API OpenAI-compatible CPU | [README](../../apps/transcription-api/README.md) |
| `apps/web` | Dashboard, API de lecture et retry opérateur | [README](../../apps/web/README.md) |
| `apps/worker-publisher` | Ingestion Exchange et publication audio | [README](../../apps/worker-publisher/README.md) |
| `apps/worker-automation` | Transcription et création GLPI | [README](../../apps/worker-automation/README.md) |
| `apps/worker-retention` | Contrôle GLPI et purge audio | [README](../../apps/worker-retention/README.md) |
| `packages/common` | Audio, backoff, attente et identité de release | [README](../../packages/common/README.md) |
| `packages/database` | Modèles, repositories et verrous PostgreSQL | [README](../../packages/database/README.md) |
| `packages/glpi-client` | Client et contrats GLPI V2.3.0 | [README](../../packages/glpi-client/README.md) |
| `providers/simulators/microsoft-graph-simulator` | Simulateur Graph de staging | [README](../../providers/simulators/microsoft-graph-simulator/README.md) |
| `providers/simulators/openai-transcription-simulator` | Simulateur synchrone OpenAI-compatible | [README](../../providers/simulators/openai-transcription-simulator/README.md) |
| `providers/simulators/transcribe-ai-simulator` | Simulateur Transcribe AI | [README](../../providers/simulators/transcribe-ai-simulator/README.md) |

Les applications n'importent pas les modules internes d'une autre application.
Le code partagé appartient aux packages.

## Répertoires structurants

| Répertoire | Rôle |
| --- | --- |
| `apps/` | Contient les six applications exécutables du projet. Leurs tests unitaires et d'intégration propres restent au plus près du code concerné. |
| `packages/` | Contient les trois bibliothèques partagées par les applications : utilitaires communs, persistance PostgreSQL et client GLPI. |
| `providers/` | Contient les simulateurs Microsoft Graph, Transcribe AI et OpenAI-compatible réservés au développement, au staging et aux tests. |
| `infra/` | Regroupe les images, les fichiers Compose et les exemples de configuration nécessaires aux différents environnements. |
| `docs/` | Contient les procédures d'exploitation, les références techniques et les conceptions futures validées. |
| `tests/` | Contient les tests transversaux de déploiement, d'intégration et de bout en bout, ainsi que leur support partagé. |
| `.github/` | Définit les workflows d'intégration continue et le modèle de Pull Request. |

Le dossier `infra/docker/` est organisé par périmètre de déploiement :

- `apps/` construit et orchestre les applications pour le staging et la
  production ;
- `development/` orchestre PostgreSQL et les services de test locaux ;
- `providers/` regroupe les images des services de test et leur orchestration
  staging ;
- `transcription/` déploie indépendamment le service local de transcription
  CPU.

## Identifiants internes stables

Le nom du projet et de ses ressources d'exploitation est `mevo2glpi`. Les
namespaces et commandes Python `mevo2glpi_*` et `mevo2glpi-*` constituent
désormais les identifiants techniques internes stables, utilisés par les
imports, les scripts console et les images.

Deux autres catégories d'identifiants ne doivent jamais être renommées par une
substitution textuelle :

- le contexte cryptographique associé aux numéros d'origine déjà chiffrés, car
  sa modification rendrait ces valeurs indéchiffrables ;
- les valeurs numériques des verrous PostgreSQL, car elles coordonnent les
  instances d'un même worker.

Les révisions Alembic et les objets de schéma existants restent également
stables. Ces identifiants relèvent de la compatibilité des données et du code,
pas de l'identité visible du projet.
