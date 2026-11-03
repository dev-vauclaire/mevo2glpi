# Package `glpi-ticketing-database`

`glpi-ticketing-database` porte les contrats SQLAlchemy partagés par le web,
les workers et l'application de migration du MVP. Il conserve le cycle de vie
durable des messages Exchange sans dépendre du client Microsoft Graph ni de la
validation WAV/OGG, qui appartiennent au worker publisher.

Le package ne contient ni point d'entrée ni historique Alembic. L'application
one-shot `apps/database-migrations` est seule responsable de l'exécution des
migrations. Les repositories sont partagés avec leurs consommateurs, séparés
par modèle, et les verrous advisory protègent l'unicité des workers persistants.

## Responsabilités

- déclarer les six tables persistantes des cycles de vie métier ;
- exposer `Base.metadata` à Alembic ;
- fournir un moteur SQLAlchemy synchrone limité à PostgreSQL avec Psycopg 3 ;
- fournir une fabrique de sessions synchrones ;
- créer et faire évoluer un job sans modifier implicitement un autre modèle ;
- enregistrer séparément l'ingestion d'un message Exchange, y compris lorsqu'il
  ne peut produire aucun job ;
- ajouter séparément des événements append-only et des politiques de
  rétention dans la transaction appartenant à l'application ;
- persister l'identifiant distant du provider legacy dès la soumission et
  l'identifiant GLPI avant la finalisation ;
- exposer le heartbeat expurgé des workers singleton ;
- exposer des agrégats bornés de lecture pour l'observabilité, sans persister
  d'alerte ;
- conserver un verrou advisory PostgreSQL sur une connexion dédiée pendant la
  durée de vie d'un worker ;
- retourner des snapshots immuables et typés plutôt que des objets ORM liés à
  une session ;
- centraliser les valeurs persistées et les contraintes structurelles.

PostgreSQL contient uniquement le cycle de vie et des informations
opérationnelles expurgées. Les octets audio restent dans le volume persistant et
aucune transcription, réponse HTTP brute ou valeur secrète n'est stockée dans
ces modèles.

## Tables

| Table | Rôle |
| --- | --- |
| `ticket_creation_jobs` | Source de vérité du traitement audio vers GLPI |
| `audio_retentions` | Planification et audit de la suppression des octets audio |
| `ticket_creation_job_events` | Historique append-only affiché par le dashboard |
| `worker_runtime_status` | Dernier heartbeat des services worker singleton |
| `mail_ingestions` | État durable et expurgé de l'acquisition d'un message Exchange |
| `mail_ingestion_events` | Historique append-only des transitions d'une ingestion |

Les valeurs d'état utilisent des colonnes `VARCHAR` et des contraintes
`CHECK`, pas des enums natifs PostgreSQL. Les UUID utilisent le type UUID natif
et tous les timestamps sont des `TIMESTAMPTZ`.

`storage_key` est une clé relative canonique de la forme
`{uuid}.{extension}`. L'extension est stockée sans point et limitée à `wav`,
`mp3`, `ogg` et `m4a`. Le code consommateur résout directement
`audio_root / storage_key` et ne parcourt jamais le volume pour retrouver un
job.

`activate()` crée une politique absente ou réactive uniquement une ligne
`inactive`. Il refuse tout état actif ou terminal afin de ne jamais écraser un
contrôle, un backoff ou le contexte durable d'une purge déjà engagée.

Une rétention `retry_scheduled` peut représenter aussi bien un nouvel appel
GLPI qu'une reprise de purge locale. Elle possède donc toujours une date de
reprise, y compris avec la politique `deadline_only`. Seul l'état `checking`
est réservé à la politique `ticket_or_deadline`.

Un retry GLPI sans contexte de suppression devient la sonde persistée de la
dépendance. Tant que cette sonde existe, les contrôles `scheduled` ordinaires
sont suspendus afin de ne pas propager une panne globale à toute la file. Les
deadlines absolues, les purges engagées et leurs retries locaux restent
prioritaires et ne sont jamais bloqués par cette barrière.

`last_glpi_status_id` stocke uniquement l'identifiant numérique positif du
statut GLPI. Le libellé n'est pas persisté car il peut être traduit. Cette
observation n'est autorisée que pour `ticket_or_deadline` ; le package database
reste indépendant du client GLPI et n'interprète pas lui-même cet entier.

Les contraintes de rétention distinguent un retry d'appel GLPI d'un retry de
purge. Une politique `deadline_only` en `retry_scheduled` ou `blocked` conserve
obligatoirement `deletion_reason` et `deletion_started_at`. Les raisons
`ticket_closed` et `deadline_expired` appartiennent à `ticket_or_deadline`,
tandis que `failed_ttl` et `blocked_ttl` appartiennent à `deadline_only`. Avant
le début d'une purge, `next_check_at` ne peut pas dépasser `hard_delete_at` ;
après ce début, un backoff peut dépasser la deadline afin d'éviter une boucle
active. Un statut `checking` porte toujours `last_checked_at`, et une
suppression confirmée ne peut pas précéder `deletion_started_at`.

L'index `(status, updated_at DESC, job_uuid DESC)` couvre les agrégats par état
du dashboard lorsque les lignes `deleted` commencent à s'accumuler. Les
deadlines et retries continuent d'utiliser leurs index partiels dédiés.

## Contrat d'ingestion des messages

`MailIngestion` et `TicketCreationJob` décrivent deux réalités distinctes. Une
ingestion représente un message source découvert dans une boîte Exchange ; un
job représente uniquement un audio valide publié pour le pipeline de création
de ticket. Un message rejeté, par exemple sans WAV/OGG valide, reste donc
observable dans `mail_ingestions` sans créer artificiellement de
`ticket_creation_jobs`.

L'identité idempotente d'une source est la paire
`(mailbox_id, message_id)` : `mailbox_id` est l'UUID de la boîte et
`message_id` l'identifiant immuable fourni par Microsoft Graph. La contrainte
unique PostgreSQL compare l'identifiant de message avec la collation `C`, donc
de manière exacte et sensible à la casse. Deux messages distincts peuvent
produire deux jobs même si leurs pièces jointes ont le même contenu. À
l'inverse, redécouvrir le même message dans la même boîte ne peut pas créer une
seconde ingestion. `attachment_id` utilise la même comparaison sensible à la
casse, mais ne participe pas à l'identité puisque le contrat métier prévoit
une seule pièce jointe audio par message.

La colonne `job_uuid` est nullable, unique et liée à
`ticket_creation_jobs.uuid` :

- une disposition `accepted` exige une pièce jointe validée et un job ;
- une disposition `rejected` exige une catégorie et un code expurgés et
  interdit tout job ;
- la disposition reste nulle tant qu'aucune décision locale durable n'a été
  prise.

`audio_extension` mémorise le format retenu (`wav` ou `ogg` pour le publisher)
dès la validation de la pièce jointe. Cette donnée permet de reprendre un
téléchargement interrompu sans redéduire son format et impose la cohérence avec
le job créé. Les extensions reconnues par le modèle partagé restent plus larges
que celles acceptées par cette application.

Une décision `accepted` ou `rejected` fait passer l'ingestion à l'étape
`acknowledgement`. Cette séparation permet à l'application appelante de
persister son résultat local avant de déplacer le message à distance. Si
l'acquittement Exchange est interrompu, l'ingestion conserve sa disposition et
peut reprendre uniquement cette étape, sans republier l'audio ni recréer le
job. L'état terminal `completed` confirme l'acquittement d'une ingestion
acceptée ; `failed` confirme celui d'un message rejeté. `blocked` signale une
continuation automatique devenue ambiguë, tandis que `retry_scheduled`
conserve la prochaine échéance et une erreur technique expurgée.

`MailIngestionRepository.resume_or_claim_next()` reprend dans cet ordre une
ingestion `processing`, un retry arrivé à échéance, puis le plus ancien message
`pending`. L'index unique partiel sur l'état `processing` matérialise le contrat
P0 d'un seul traitement publisher à la fois. Un retry de portée `system`
suspend l'acquisition de nouveaux messages en attente afin de ne pas propager
une panne commune à toute la file.

`MailIngestionEvent` conserve l'audit append-only de ces transitions. Il ne
stocke que les UUID, états, étapes, dispositions et erreurs expurgées : ni
sujet, ni expéditeur, ni corps du message, ni contenu audio, ni réponse HTTP
brute. Son repository n'altère jamais `MailIngestion`.

Ces modèles constituent le contrat de persistance utilisé par le client
Microsoft Graph, la validation WAV/OGG et l'acquittement Exchange orchestrés
dans l'application `worker-publisher`.

Le rejeu opérateur des ingestions est volontairement séparé du retry des jobs.
`MailIngestionRepository.list_replay_candidates()` applique un filtre borné et
`rearm_for_operator_replay()` verrouille puis revalide chaque ligne avant de la
remettre à `pending/discovery`. Il conserve l'identité source, les compteurs et
le premier démarrage, mais efface les métadonnées et décisions dérivées afin que
le publisher refasse la validation depuis le message source. L'application
ajoute séparément l'événement append-only `operator_replay_requested` dans la
même transaction ; le repository ne crée jamais cet événement lui-même.

## Cycle de vie du worker d'automatisation

`TicketCreationJobRepository.resume_or_claim_next()` sélectionne atomiquement
un seul job dans cet ordre :

1. le job `processing` à reprendre après un arrêt ;
2. le plus ancien `retry_scheduled` arrivé à échéance ;
3. le plus ancien `pending`.

Un retry de portée `system` non encore échu suspend la réclamation de nouveaux
jobs `pending`, afin de ne pas propager une même indisponibilité globale à
toute la file. Ce repository ne crée ni événement ni rétention. Le service
applicatif du worker coordonne `TicketCreationJobRepository`,
`TicketCreationJobEventRepository` et `AudioRetentionRepository` dans une même
transaction.

Le repository ne déplace aucun fichier et ne scanne aucun répertoire. Il
persiste uniquement la `storage_key` canonique ; le consommateur résout ensuite
le chemin exact avec le package commun.

Les repositories n'appellent jamais `commit()`. Ils valident et `flush()` les
mutations dans la session fournie, ce qui laisse la frontière transactionnelle
au service appelant.

Cette convention vaut également pour les ingestions :
`MailIngestionRepository` ne crée ni `TicketCreationJob` ni événement, et
`MailIngestionEventRepository` ne modifie que `MailIngestionEvent`. La création
atomique d'un job, de son événement `JOB_CREATED`, du lien d'ingestion et de
l'événement d'ingestion appartient au service applicatif appelant.

## Lecture dashboard et relance opérateur

`TicketCreationJobRepository.list_page()` expose une pagination keyset stable
sur `(updated_at, uuid) DESC`, avec filtre d'état et recherche exacte par UUID.
`TicketCreationJobEventRepository.list_page()` applique la même convention sur
`(occurred_at, event_id) DESC`. Les curseurs HTTP opaques appartiennent à
l'application web : le package database ne manipule que leurs valeurs typées.

Les lectures d'alerte restent également séparées par modèle :

- `TicketCreationJobRepository.get_alert_summary()` compte les jobs
  `failed`, `blocked`, `pending` trop anciens et les retries en retard ;
- `AudioRetentionRepository.get_alert_summary()` distingue deadline dépassée,
  contrôle GLPI bloqué, purge bloquée et retries en retard ;
- `WorkerRuntimeStatusRepository.list_all()` retourne les heartbeats présents,
  sans fabriquer lui-même les états temporels `fresh`, `stale` ou `absent`.

Chaque agrégat contient un compteur et le plus ancien timestamp source. Les
seuils, priorités, délais de grâce et clés de déduplication appartiennent au
service web. Les comparaisons de cutoff sont strictes et utilisent des dates
UTC. Une ligne de rétention contribue à une seule condition, avec la priorité
deadline, blocage, puis retry. Aucune table `alerts` ni transaction d'écriture
n'est nécessaire pour construire cet instantané.

La relance d'un job reste une orchestration applicative. Dans une même
transaction, le web appelle successivement :

1. `TicketCreationJobRepository.rearm_for_operator_retry()` ;
2. `AudioRetentionRepository.deactivate_for_operator_retry()` ;
3. `TicketCreationJobEventRepository.append()` avec
   `operator_retry_requested`.

Le réarmement accepte uniquement `failed` ou `blocked`, remet les champs
transitoires à zéro et conserve l'historique : UUID, `glpi_ticket_id`, compteur
total de tentatives, création et premier démarrage. La rétention ne peut être
désactivée que depuis `scheduled`, `retry_scheduled` ou `blocked`. Les lignes
sont verrouillées dans l'ordre job puis rétention ; un repository ne modifie
jamais le modèle du suivant.

Même depuis `retry_scheduled` ou `blocked`, la désactivation est refusée si
`deletion_started_at` est déjà renseigné. À ce stade, l'audio peut avoir quitté
son emplacement canonique et seul le flux de purge sait reprendre l'opération
sans perdre son contexte.

## Verrous advisory et runtime

`try_acquire_advisory_lock()` réserve une clé stable par
`WorkerServiceName` sur une connexion dédiée. Le handle retourné permet de
vérifier que la même session PostgreSQL détient toujours le verrou et de le
libérer explicitement. Si le verrou est déjà pris, la fonction retourne
`None` ; aucune attente implicite n'est effectuée.

Le worker revalide ce handle aux frontières transactionnelles et externes. Une
connexion perdue ou un backend PostgreSQL différent invalide la propriété : le
processus échoue alors en mode fail-closed au lieu de poursuivre sous une
unicité devenue incertaine. `AdvisoryLock.open_session()` lie en plus les
transactions métier à cette connexion propriétaire : perdre le verrou fait
également échouer le commit, avant qu'une instance obsolète puisse publier une
transition locale.

`WorkerRuntimeStatusRepository.record_heartbeat()` remplace la ligne du
service singleton avec son instance, son état, son travail courant éventuel et
des codes d'erreur expurgés. Les états `degraded` et `blocked` exigent une erreur ;
les autres états ne peuvent pas en stocker. Seul un heartbeat `starting`, écrit
après acquisition du verrou advisory, peut remplacer l'identifiant d'une
ancienne instance. Tout heartbeat ultérieur portant un autre identifiant est
refusé afin qu'un processus supplanté ne puisse pas écraser l'état du nouveau.
Cette cohérence erreur/état est également imposée par PostgreSQL afin qu'une
écriture extérieure au repository ne puisse produire un snapshot ambigu.

Le travail courant est typé par service : `worker_automation` et
`worker_retention` peuvent référencer `current_job_uuid`, tandis que
`worker_publisher` peut référencer `current_ingestion_uuid`. Les deux clés sont
nullables et utilisent `ON DELETE SET NULL`, mais PostgreSQL interdit qu'un
service renseigne la mauvaise clé. Le dashboard projette les trois heartbeats,
dont le travail publisher via `current_ingestion_uuid`, sans charger de donnée
métier Exchange sensible.

## Transactions

Une `Session` SQLAlchemy constitue l'unité de travail. Les applications ouvrent
des transactions courtes avec la fabrique exposée :

```python
from glpi_ticketing_database import (
    JobEventActor,
    JobEventType,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
    create_database_engine,
    create_session_factory,
)

engine = create_database_engine(database_url)
session_factory = create_session_factory(engine)

with session_factory.begin() as session:
    snapshot = TicketCreationJobRepository(session).add_pending(pending_job)
    TicketCreationJobEventRepository(session).append(
        snapshot.uuid,
        occurred_at=snapshot.created_at,
        status=snapshot.status,
        stage=snapshot.stage,
        event_type=JobEventType.JOB_CREATED,
        actor=JobEventActor.WORKER_PUBLISHER,
    )
```

Aucune responsabilité inter-modèles n'est cachée dans un repository : dans le
flux réel, le publisher orchestre explicitement la création du job, de son
événement et du lien d'ingestion dans cette même transaction.

Aucune transaction ne doit rester ouverte pendant un appel GLPI, un appel au
provider de transcription ou une attente de polling.

## Configuration

L'URL doit utiliser explicitement le pilote synchrone Psycopg 3 :

```text
postgresql+psycopg://user:password@postgres:5432/glpi_ticketing
```

La fonction de création n'inclut jamais l'URL reçue dans ses erreurs de
validation. Les applications restent responsables de ne pas journaliser la
configuration brute. Le paramètre optionnel `connect_timeout_seconds` transmet
un délai de connexion à Psycopg sans l'ajouter à l'URL ; l'application de
migration le renseigne afin que chaque tentative de readiness reste bornée.

## Arborescence

```text
packages/database/
├── pyproject.toml
├── README.md
├── src/glpi_ticketing_database/
│   ├── __init__.py
│   ├── advisory_locks.py
│   ├── engine.py
│   ├── repositories/
│   │   ├── __init__.py
│   │   ├── _alerting.py
│   │   ├── _validation.py
│   │   ├── ticket_creation_job.py
│   │   ├── ticket_creation_job_event.py
│   │   ├── audio_retention.py
│   │   ├── mail_ingestion.py
│   │   ├── mail_ingestion_event.py
│   │   └── worker_runtime_status.py
│   └── models/
│       ├── __init__.py
│       ├── base.py
│       ├── enums.py
│       ├── ticket_creation_job.py
│       ├── audio_retention.py
│       ├── ticket_creation_job_event.py
│       ├── mail_ingestion.py
│       ├── mail_ingestion_event.py
│       └── worker_runtime_status.py
└── tests/
    ├── unit/
    │   ├── test_advisory_locks.py
    │   ├── test_alert_repositories.py
    │   ├── test_audio_retention_acquisition.py
    │   ├── test_audio_retention_repository.py
    │   ├── test_audio_retention_transitions.py
    │   ├── test_engine.py
    │   ├── test_mail_ingestion_event_repository.py
    │   ├── test_mail_ingestion_repository.py
    │   ├── test_models.py
    │   ├── test_repository_boundaries.py
    │   ├── test_ticket_creation_job_event_repository.py
    │   ├── test_ticket_creation_job_repository.py
    │   ├── test_ticket_creation_job_transitions.py
    │   └── test_worker_runtime_status_repository.py
    └── integration/
        ├── test_audio_retention_repository.py
        ├── test_dashboard_repositories.py
        ├── test_mail_ingestion_repository.py
        ├── test_observability_repositories.py
        └── test_ticket_creation_job_repository.py
```

## Validation depuis la racine

```bash
uv run pytest packages/database/tests/unit
uv run pytest -m database packages/database/tests/integration -vv --maxfail=1
uv run ruff format --check packages/database
uv run ruff check packages/database
```

Les contraintes réellement dépendantes de PostgreSQL, les verrous session et
les transitions atomiques sont couverts par la fixture commune
`tests/integration/postgres` et par les suites d'intégration des applications.
