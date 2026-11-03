# Conception de la rétention de l'historique PostgreSQL

> Statut : conception validée, implémentation différée.
>
> Ce document décrit la politique retenue pour une future itération. Le code
> correspondant n'est pas présent dans l'état actuel du dépôt. Il a été retiré
> afin d'aligner d'abord la documentation générale avec le fonctionnement réel
> de l'application.

## Objectif

Empêcher la croissance illimitée de PostgreSQL tout en conservant un délai
d'exploitation suffisant après la suppression d'un audio.

Le worker de rétention existant portera cette responsabilité. Aucun service
supplémentaire ne sera créé.

## Politique validée

- conservation des données pendant **72 heures** après leur date d'ancrage ;
- balayage PostgreSQL toutes les **24 heures** ;
- traitement de **100 agrégats au maximum** par balayage ;
- premier déploiement en mode `report`, sans suppression ;
- passage explicite en mode `apply` après validation des compteurs et
  sauvegarde de PostgreSQL ;
- perte de l'idempotence Exchange acceptée après la purge d'une ingestion ;
- aucun opérateur ne doit remettre manuellement d'anciens messages dans Inbox.

## Données éligibles

Deux catégories sont concernées :

1. un job dont l'audio a été supprimé avec succès :
   `database_purge_at = audio_deleted_at + 72 heures` ;
2. une ingestion Exchange rejetée sans création de job :
   `database_purge_at = finished_at + 72 heures`.

Les jobs actifs, les retries, les audios encore conservés et les blocages non
résolus ne doivent jamais être purgés.

## Évolution du schéma

Une migration Alembic devra ajouter une colonne nullable
`database_purge_at` dans :

- `audio_retentions` ;
- `mail_ingestions`.

La migration devra également :

- calculer l'échéance des lignes terminales déjà présentes à partir de leur
  horodatage historique, et non de la date du déploiement ;
- ajouter des contraintes empêchant d'associer une échéance à un état actif ;
- ajouter des index PostgreSQL partiels sur les lignes terminales planifiées ;
- rester réversible sans supprimer les données métier lors du downgrade.

## Suppression transactionnelle

Chaque agrégat sera supprimé dans une transaction courte et indépendante. Les
repositories resteront limités à un seul modèle et ne réaliseront aucun
`commit`.

Ordre prévu pour un job issu d'un message accepté :

1. événements de l'ingestion ;
2. ingestion Exchange ;
3. événements du job ;
4. rétention audio ;
5. job.

Ordre prévu pour une ingestion rejetée sans job :

1. événements de l'ingestion ;
2. ingestion Exchange.

Les clés étrangères resteront en `RESTRICT`. Aucun `ON DELETE CASCADE` ne sera
ajouté. Une erreur devra annuler l'intégralité de la transaction de l'agrégat.

## Modes d'exploitation

Les variables prévues sont :

```dotenv
RETENTION_DATABASE_MODE=report
RETENTION_DATABASE_TTL_HOURS=72
RETENTION_DATABASE_SWEEP_INTERVAL_HOURS=24
RETENTION_DATABASE_BATCH_SIZE=100
```

Valeurs de `RETENTION_DATABASE_MODE` :

- `disabled` : aucun balayage ;
- `report` : lecture et compteurs uniquement ;
- `apply` : suppression transactionnelle des agrégats échus.

Les journaux du balayage ne devront contenir que des compteurs agrégés et
l'indication d'un lot saturé. Ils ne devront exposer aucun UUID, identifiant
Graph, ticket GLPI, transcription ou contenu audio.

## Stratégie de validation

Les tests devront couvrir au minimum :

- migration d'une base neuve et d'une base existante ;
- calcul et backfill des échéances à 72 heures ;
- contraintes et index partiels ;
- absence totale d'écriture en mode `report` ;
- lot strictement limité à 100 agrégats avec détection d'un reliquat ;
- ordre des suppressions et absence de ligne orpheline ;
- rollback transactionnel lorsqu'une suppression intermédiaire échoue ;
- exclusion des états actifs, bloqués ou dont l'audio existe encore ;
- absence de données sensibles dans les journaux ;
- configuration des environnements staging et production.

## Mise en service future

1. déployer la migration et le worker en mode `report` sur staging ;
2. contrôler les compteurs agrégés pendant au moins un cycle ;
3. sauvegarder PostgreSQL ;
4. activer `apply` sur staging et vérifier l'intégrité des agrégats restants ;
5. répéter la procédure en production, d'abord en `report`, puis en `apply` ;
6. laisser l'autovacuum récupérer l'espace réutilisable ; ne jamais automatiser
   `VACUUM FULL` dans le worker.

Un retour arrière du code pourra désactiver le balayage. Les lignes déjà
supprimées en mode `apply` ne pourront être restaurées qu'à partir de la
sauvegarde PostgreSQL.
