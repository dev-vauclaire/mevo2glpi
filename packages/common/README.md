# Package `glpi-ticketing-common`

`glpi-ticketing-common` contient les primitives de stockage audio partagées par
le web et le worker d'automatisation. Il ne connaît ni PostgreSQL, ni FastAPI,
ni GLPI, ni Transcribe AI et ne porte aucune politique métier.

## Stockage canonique unique

Le MVP conserve tous les octets audio dans une seule racine configurable,
`/data/audios` dans le déploiement Docker. Un changement d'état du job ne
déplace jamais son fichier : `pending`, `processing`, `completed`, `failed` et
`blocked` sont exclusivement des valeurs PostgreSQL.

La base stocke une clé relative immuable :

```text
{uuid-canonique}.{extension-minuscule}
```

Exemple :

```text
123e4567-e89b-42d3-a456-426614174000.m4a
```

Les extensions acceptées sont `wav`, `mp3`, `ogg` et `m4a`.

```mermaid
flowchart LR
    UUID[UUID + extension] --> Key[Clé canonique]
    Key --> Database[(PostgreSQL<br/>storage_key)]
    Key --> Path[/data/audios/clé]
    Stream[Flux upload] --> Part[Fichier .part unique]
    Part --> Sync[flush + fsync]
    Sync --> Publish[Lien create-only atomique]
    Publish --> Path
```

## API publique

L'API se trouve dans `glpi_ticketing_common.audio_files`.

| Primitive | Responsabilité |
| --- | --- |
| `build_audio_storage_key()` | Normaliser l'extension à la frontière d'upload et construire la clé |
| `parse_audio_storage_key()` | Refuser toute clé persistée non canonique ou traversée de chemin |
| `resolve_audio_storage_path()` | Résoudre un chemin exact, confiné à la racine et non symbolique |
| `publish_audio()` | Publier un flux sans écrasement, avec mémoire bornée et durabilité disque |
| `delete_audio()` | Supprimer exactement une clé puis synchroniser le répertoire |
| `parse_audio_filename()` | Valider le nom canonique attendu par l'adaptateur Transcribe AI |

`parse_audio_filename()` accepte une extension en majuscules à la frontière du
provider et retourne une représentation normalisée. En revanche,
`parse_audio_storage_key()` n'effectue aucune correction silencieuse : une clé
relue depuis PostgreSQL doit déjà être canonique.

## Garanties de publication

`publish_audio()` :

1. valide la racine et la clé ;
2. écrit le flux par blocs de 64 Kio dans un `.part` unique situé dans la même
   racine ;
3. exécute `flush` puis `fsync` sur le fichier ;
4. publie le nom final avec un lien dur create-only ;
5. synchronise le répertoire ;
6. retire le fichier final et le `.part` si la durabilité ne peut pas être
   confirmée.

Une destination existante n'est jamais remplacée, y compris lors d'une course
entre deux publications. La primitive interne `_atomic_write()` reste un détail
d'implémentation : les applications utilisent uniquement `publish_audio()` afin
que la validation de `storage_key` ne puisse pas être contournée.

`delete_audio()` retourne `False` lorsque la clé est déjà absente. Toute autre
erreur disque reste observable afin que l'appelant ne considère pas une
suppression incertaine comme réussie.

## Conventions

- ne jamais retrouver un audio avec un scan, `glob` ou `iterdir` ;
- résoudre `storage_key` directement sous la racine configurée ;
- ne jamais encoder un état métier dans un répertoire ;
- ne pas exposer de primitive acceptant un chemin de destination arbitraire ;
- conserver les écritures volumineuses en streaming borné ;
- laisser à l'application la transaction PostgreSQL et les décisions de retry
  ou de rétention.

## Exemple

```python
from io import BytesIO
from pathlib import Path
from uuid import UUID

from glpi_ticketing_common.audio_files import (
    build_audio_storage_key,
    publish_audio,
)

audio_uuid = UUID("123e4567-e89b-42d3-a456-426614174000")
storage_key = build_audio_storage_key(audio_uuid, ".WAV")
published = publish_audio(
    Path("/data/audios"),
    storage_key,
    BytesIO(b"audio"),
)
```

## Arborescence et validation

```text
packages/common/
├── pyproject.toml
├── README.md
├── src/glpi_ticketing_common/
│   ├── __init__.py
│   └── audio_files.py
└── tests/unit/
    └── test_audio_storage.py
```

Depuis la racine :

```bash
uv run pytest packages/common/tests
uv run ruff format --check packages/common
uv run ruff check packages/common
```
