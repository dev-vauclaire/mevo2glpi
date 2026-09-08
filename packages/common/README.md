# Package `glpi-ticketing-common`

`glpi-ticketing-common` contient les primitives de stockage audio, de backoff,
d'attente coopérative et de healthcheck runtime partagées par le web et les
trois workers. Il ne connaît ni PostgreSQL, ni FastAPI, ni GLPI, ni Transcribe AI
et ne porte aucune politique métier.

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
    Stream[Flux audio entrant] --> Part[Fichier .part unique]
    Part --> Sync[flush + fsync]
    Sync --> Publish[Lien create-only atomique]
    Publish --> Path
```

## API publique

Les primitives de stockage courant se trouvent dans
`glpi_ticketing_common.audio_files`.

| Primitive | Responsabilité |
| --- | --- |
| `build_audio_storage_key()` | Normaliser l'extension à la frontière d'ingestion et construire la clé |
| `parse_audio_storage_key()` | Refuser toute clé persistée non canonique ou traversée de chemin |
| `resolve_audio_storage_path()` | Résoudre un chemin exact, confiné à la racine et non symbolique |
| `publish_audio()` | Publier un flux sans écrasement, avec mémoire bornée et durabilité disque |
| `delete_audio()` | Supprimer exactement une clé puis synchroniser le répertoire |
| `parse_audio_filename()` | Valider le nom canonique attendu par l'adaptateur Transcribe AI |

`glpi_ticketing_common.audio_purge` expose une suppression spécialisée pour le
worker de rétention. `purge_audio()` valide une clé canonique exacte, publie le
nom `.purge/{storage_key}` de façon create-only par lien dur sur le même volume,
retire le nom canonique, synchronise les deux répertoires puis supprime les
octets. Elle reprend de manière idempotente si un crash a laissé les deux noms
du même inode, le seul nom en quarantaine ou des octets déjà absents. Deux noms
pointant vers des inodes distincts restent ambigus. Elle refuse également les
liens symboliques, entrées non régulières et chemins non canoniques.

Cette primitive cible le volume Linux local du déploiement Docker : la racine
et `.purge` doivent partager le même système de fichiers, avec prise en charge
des liens durs et de `fsync` sur les répertoires.

`glpi_ticketing_common.backoff.ExponentialBackoffPolicy` calcule un délai
exponentiel plafonné sans dormir ni connaître le mécanisme de persistance. Le
worker appelant reste responsable du compteur consécutif et de la date de
prochaine tentative.

`glpi_ticketing_common.cooperative_wait.wait_with_periodic_checkpoint()`
découpe une attente en tranches bornées. Entre deux tranches, elle vérifie
l'arrêt demandé puis appelle un checkpoint injecté. Les workers l'utilisent
pour rafraîchir leur heartbeat sur la connexion qui détient leur verrou
advisory. La primitive ne crée aucun thread, ne connaît ni PostgreSQL ni le
heartbeat et propage toute erreur du checkpoint afin que l'application puisse
échouer en mode fermé.

`glpi_ticketing_common.runtime_healthcheck` porte la politique générique des
sondes conteneur sans importer SQLAlchemy : configuration minimale, fraîcheur
du heartbeat, codes d'erreur expurgés et vérification du mode d'accès au volume
audio. Chaque worker fournit son propre adaptateur PostgreSQL et choisit le
contrat attendu : lecture seule pour l'automatisation, lecture-écriture pour le
publisher et la rétention. Le seuil `WORKER_HEALTHCHECK_STALE_AFTER_SECONDS`
vaut `45` secondes
par défaut et doit rester strictement supérieur à
`WORKER_HEARTBEAT_INTERVAL_SECONDS`.

Le volume Docker canonique est monté en lecture-écriture par le publisher et le
worker de rétention, et en lecture seule par le web et le worker d'automatisation. Le
[déploiement Dokploy](../../README.md#déploiement-dokploy) décrit son
volume persistant et sa configuration.

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
│   ├── audio_files.py
│   ├── audio_purge.py
│   ├── backoff.py
│   ├── cooperative_wait.py
│   └── runtime_healthcheck.py
└── tests/unit/
    ├── test_audio_storage.py
    ├── test_audio_purge.py
    ├── test_backoff.py
    ├── test_cooperative_wait.py
    └── test_runtime_healthcheck.py
```

Depuis la racine :

```bash
uv run pytest packages/common/tests
uv run ruff format --check packages/common
uv run ruff check packages/common
```
