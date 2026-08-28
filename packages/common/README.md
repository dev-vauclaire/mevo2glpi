# Package `glpi-ticketing-common`

`glpi-ticketing-common` rassemble les primitives de stockage audio utilisées par
l'application web et le worker. Il ne contient ni règle GLPI, ni appel HTTP, ni
configuration d'environnement : les applications gardent la responsabilité de
leur orchestration et de leurs politiques métier.

Le package est un membre du workspace `uv` et est résolu localement grâce à
`[tool.uv.sources]` dans le `pyproject.toml` racine.

## Entrées et sorties

L'API actuelle est concentrée dans `glpi_ticketing_common.audio_files`.

| Primitive | Entrée | Sortie | Responsabilité |
| --- | --- | --- | --- |
| `parse_audio_filename()` | Un nom de fichier sans chemin | `AudioFileName` | Valider et normaliser l'UUID et l'extension |
| `atomic_write()` | Une destination et un flux binaire | Le chemin publié | Écrire sans exposer un fichier partiel |
| `build_audio_storage_key()` | UUID et extension d'upload | Clé canonique | Normaliser uniquement à la frontière d'upload |
| `parse_audio_storage_key()` | Clé persistée | `AudioStorageKey` | Exiger un UUID et une extension déjà canoniques |
| `publish_audio()` | Racine, clé et flux | `PublishedAudio` | Publier sans écrasement et compter les octets |
| `delete_audio()` | Racine et clé | booléen | Supprimer un chemin exact puis synchroniser le volume |
| `move_audio()` | Un fichier source et un répertoire cible | Le chemin cible | Copier entre volumes puis supprimer la source |
| `move_to_failed()` | Un audio, `failed` et un objet JSON | `FailedTransition` | Publier l'audio et son sidecar avant de supprimer la source |
| `list_ready_audio_files()` | Un répertoire existant | Une liste triée de chemins | Retourner uniquement les audios complets et valides |

Les extensions acceptées sont `.wav`, `.mp3`, `.ogg` et `.m4a`. Un audio géré
par le pipeline utilise un UUID sous sa forme hyphénée et respecte la convention
suivante :

```text
{uuid-canonique}.{extension}
```

Exemple :

```text
123e4567-e89b-42d3-a456-426614174000.m4a
```

La casse de l'extension est acceptée uniquement lors de la construction depuis
un upload. Une clé relue depuis PostgreSQL doit déjà être canonique : toute
normalisation silencieuse, traversée, sous-répertoire ou destination symbolique
est refusée.

## Publication d'un fichier

```mermaid
flowchart LR
    A[Flux binaire] --> B[Écriture par blocs dans un fichier .part unique]
    B --> C[flush et fsync du fichier]
    C --> D[Lien atomique create-only vers le nom final]
    D --> E[fsync du répertoire cible]
    E --> F[Fichier visible par les scanners]
```

Les primitives écrivent par blocs de 64 Kio par défaut. Le fichier temporaire
reste dans le même répertoire que sa destination. Un lien dur publie ensuite le
nom final de façon atomique et échoue si ce nom existe déjà ; une course ne peut
donc jamais écraser un audio. Un scanner historique peut toujours ignorer les
fichiers `.part` et ne jamais lire un contenu incomplet.

La primitive refuse d'écraser une destination existante. En cas d'échec, elle
supprime ses artefacts temporaires et ne publie pas de résultat partiel.

## Déplacement entre volumes

Un renommage atomique n'est pas garanti entre deux volumes. `move_audio()`
applique donc la séquence de sûreté suivante :

1. valider le fichier source ;
2. publier une copie complète et synchronisée dans le répertoire cible ;
3. supprimer la source ;
4. synchroniser le répertoire source.

Si la copie échoue, la source reste disponible. Si sa suppression échoue alors
qu'elle existe encore, la copie cible est retirée afin d'éviter deux fichiers
présentés comme actifs.

Pour une transition vers `failed`, `move_to_failed()` publie d'abord l'audio,
puis `{uuid}.error.json`, et ne supprime l'audio source qu'après la publication
complète des deux fichiers. L'appelant est responsable de fournir un payload
déjà expurgé : ce package garantit une sérialisation JSON stricte, mais ne peut
pas déterminer quelles données métier sont sensibles.

## Arborescence et conventions

```text
packages/common/
├── pyproject.toml
├── README.md
├── src/glpi_ticketing_common/
│   ├── __init__.py
│   └── audio_files.py
└── tests/unit/
    ├── test_audio_files.py
    └── test_audio_storage.py
```

Conventions du package :

- conserver ici uniquement les primitives réellement partagées par plusieurs
  applications ;
- ne pas dépendre des packages `glpi_ticketing_web` ou
  `glpi_ticketing_worker_automation` ;
- laisser à l'appelant la création des répertoires et la configuration de leurs
  permissions ;
- effectuer les écritures volumineuses en streaming avec une mémoire bornée ;
- refuser les écrasements implicites et préserver la source tant que la cible
  n'est pas complètement publiée ;
- tester l'ordre des opérations disque et les scénarios d'échec injectés, pas
  seulement le résultat nominal.

Le POC suppose actuellement un seul writer pour un UUID donné. L'ajout de
plusieurs workers demanderait une stratégie de réclamation et de verrouillage
supplémentaire.

## Utilisation

```python
from pathlib import Path

from glpi_ticketing_common.audio_files import move_audio

processing_audio = move_audio(
    Path("/data/audio-pending/123e4567-e89b-42d3-a456-426614174000.m4a"),
    Path("/data/audio-processing"),
)
```

Les deux répertoires doivent déjà exister. Le nom de la source doit respecter le
contrat UUID et extension décrit plus haut.

## Commandes depuis la racine

Après l'installation du workspace :

```bash
uv run pytest packages/common/tests
uv run ruff format --check packages/common
uv run ruff check packages/common
```

Pour exécuter toutes les validations du dépôt, utiliser les commandes décrites
dans le [README racine](../../README.md).
