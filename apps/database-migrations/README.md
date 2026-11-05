# Application database-migrations

Cette application uv exécute les migrations Alembic du MVP puis se termine.
Elle ne sert aucune requête et ne possède pas les modèles SQLAlchemy : ceux-ci
restent dans le package partagé `glpi-ticketing-database`.

## Contrat d'exécution

La variable `DATABASE_URL` est obligatoire et doit utiliser le dialecte
synchrone `postgresql+psycopg`. L'application tente de joindre PostgreSQL
pendant la fenêtre de readiness, exécute `upgrade head`, puis retourne `0`.
Après l'expiration de cette fenêtre, une tentative de connexion déjà engagée
peut encore durer jusqu'au timeout de connexion configuré. Une configuration
invalide, une base indisponible ou une migration en échec produit un code non
nul sans journaliser l'URL, son mot de passe ou le détail brut de l'exception.

Variables optionnelles :

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `APP_ENVIRONMENT` | `development` | Environnement `development`, `staging` ou `production` de l'identité déployée |
| `APP_RELEASE` | `development` | `main` ou tag `vX.Y.Z`, selon l'environnement |
| `APP_COMMIT_SHA` | `unknown` | SHA Git complet, obligatoire hors développement local |
| `DATABASE_READY_TIMEOUT_SECONDS` | `60` | Fenêtre pendant laquelle relancer PostgreSQL |
| `DATABASE_READY_INTERVAL_SECONDS` | `1` | Intervalle entre deux essais |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | `5` | Limite de chaque connexion, entre 1 et 60 secondes |

Lancement depuis la racine du workspace :

```bash
uv run --package glpi-ticketing-database-migrations \
  glpi-ticketing-database-migrations
```

La forme module est équivalente :

```bash
uv run --package glpi-ticketing-database-migrations \
  python -m glpi_ticketing_database_migrations
```

Les ressources Alembic sont embarquées dans le package Python. Leur résolution
ne dépend donc pas du répertoire courant du processus.

## Image et déploiement

Le [Dockerfile de migration](../../infra/docker/apps/database-migrations/Dockerfile)
construit une image multi-stage verrouillée par `uv.lock`, sans dépendances de
développement ni installation editable. Le runtime non-root exécute uniquement
`glpi-ticketing-database-migrations`, puis s'arrête : le service Compose est un
one-shot sans healthcheck ni politique de redémarrage.

Cette application accède seulement à PostgreSQL. Elle ne monte pas le volume
audio `/data/audios`. Elle attend elle-même la base externe dans un délai borné
et sa réussite conditionne le démarrage des applications persistantes du MVP.
Les ordres de démarrage et les contrôles opérateur sont décrits dans les
procédures de [staging](../../docs/procedures/staging.md) et de
[production](../../docs/procedures/production.md).

## Historique courant

- `0001_initial_schema` crée les quatre tables du cycle de vie MVP ;
- `0002_add_operator_retry_event` étend la contrainte des événements avec
  `operator_retry_requested`, sans réécrire la migration initiale ;
- `0003_add_retention_lifecycle` remplace le libellé GLPI par son identifiant
  numérique, ajoute les invariants du cycle de purge et autorise les quatre
  événements du worker de rétention ;
- `0004_add_observability_queries` ajoute l'index de lecture des rétentions par
  état et impose la cohérence entre état runtime et erreur expurgée ;
- `0005_add_mail_ingestions` crée `mail_ingestions` et
  `mail_ingestion_events`, puis autorise `worker_publisher` comme acteur des
  jobs et comme service de runtime. Le runtime peut référencer l'ingestion
  courante via `current_ingestion_uuid`, sans détourner la clé de job utilisée
  par les deux workers en aval.
- `0006_add_ogg_replay_support` persiste l'extension audio de l'ingestion,
  ajoute les invariants nécessaires au rejeu contrôlé d'un rejet et autorise
  l'événement d'audit `operator_replay_requested` ;
- `0007_transcription_failed_event` autorise l'événement d'audit expurgé
  `transcription_failed` lorsque la création du ticket continue malgré un
  échec terminal de transcription ;
- `0008_originating_phone_cipher` ajoute le ciphertext temporaire et borné
  du numéro d'origine, sans stocker l'objet Exchange ni le numéro en clair.

L'historique est compatible avec les deux situations de déploiement attendues :

- sur une base vide, `upgrade head` applique l'historique complet et crée les
  six tables du MVP ;
- sur une base existante à partir de la révision `0004`, la montée
  conserve les jobs, événements, rétentions, heartbeats et ingestions déjà
  présents.

Relancer `upgrade head` après cette montée est un no-op Alembic et conserve les
données existantes. La migration ajoute une identité de source unique composée
de l'UUID de boîte et de l'identifiant de message. Ce dernier utilise la
collation PostgreSQL `C` afin que l'idempotence respecte la casse des
identifiants immuables Exchange.

La migration ne crée volontairement aucune ingestion à partir des jobs
historiques : une ingestion Exchange et un job audio sont deux concepts
distincts, et les lignes antérieures à l'introduction du publisher n'ont pas de
source Exchange fiable à reconstruire. Les nouvelles ingestions peuvent rester
sans job lorsqu'un message est rejeté ; une ingestion acceptée lie au contraire
un unique `TicketCreationJob` avant l'étape durable d'acquittement.

`0006` recopie `audio_extension` depuis le job pour les ingestions acceptées et
marque `wav` les anciennes lignes ayant déjà atteint une étape où seul ce format
était accepté. Les rejets de métadonnées qui n'avaient jamais sélectionné de
fichier conservent une extension nulle. La migration ne réarme aucun rejet : le
choix du lot et l'audit restent une action explicite de l'opérateur via la
commande du publisher.

Les montées depuis `0001` et `0002` sont testées avec des données
préexistantes. `0003` convertit explicitement les valeurs numériques de
`last_glpi_status` vers un `SMALLINT` positif et son downgrade restaure leur
représentation texte. Une ancienne valeur non vide et non numérique fait
échouer la migration au lieu d'être perdue silencieusement.

Les downgrades des migrations d'événements conservent volontairement la
sécurité native de PostgreSQL : ils échouent si des événements plus récents
existent encore, au lieu de supprimer silencieusement l'historique d'audit.
De même, `0004` refuse une donnée runtime antérieure incohérente au lieu de
masquer ou réécrire son erreur pendant la montée de version.

Le downgrade de `0006` est également refusé tant qu'une ingestion non-WAV se
trouve à l'étape `publication`. Supprimer alors `audio_extension` ferait
reprendre ce fichier comme un WAV par l'ancienne version du publisher. Il faut
d'abord laisser la version courante terminer ou isoler explicitement cette
ingestion ; la migration ne dégrade jamais silencieusement son format.

Le downgrade de `0005` supprime les tables propres à l'ingestion. Avant cette
suppression, il restaure les contraintes d'acteurs et de services antérieures ;
PostgreSQL refuse donc le downgrade si des événements de job ou des heartbeats
`worker_publisher` doivent encore être conservés. Aucun enregistrement d'audit
historique n'est réécrit silencieusement.

## Tests

Les tests unitaires ne nécessitent pas PostgreSQL :

```bash
uv run pytest apps/database-migrations/tests/unit
```

La compatibilité de la migration est vérifiée explicitement sur une base vide
et sur une base existante arrêtée à `0004`, préchargée avec un job, son
événement, sa rétention et les heartbeats des workers existants :

```bash
uv run pytest -m database \
  apps/database-migrations/tests/integration/test_mail_ingestion_migration.py \
  -vv --maxfail=1
```

La fixture commune `tests/integration/postgres` démarre une image
PostgreSQL 16 épinglée par digest. Elle l'expose sur un port aléatoire limité au
loopback, crée une base isolée par test, vérifie la migration et les contraintes
réelles, puis détruit le conteneur et son réseau. Elle est partagée avec les
tests d'intégration du package database et du web et ne doit pas être
parallélisée :

```bash
uv run pytest -m database \
  apps/database-migrations/tests/integration \
  apps/web/tests/integration \
  -vv --maxfail=1
```
