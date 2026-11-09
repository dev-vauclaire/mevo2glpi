# Journal des changements

Ce fichier recense les changements fonctionnels et opérationnels destinés aux
équipes qui évaluent ou déploient mevo2glpi. La version du produit est portée
par les tags Git immuables `vX.Y.Z` ; les versions `0.1.0` des packages Python
sont des métadonnées internes au workspace et ne constituent pas des releases.

## v0.10.0 — 2026-11-09

### Ajouté

- vue paginée des messages Exchange rejetés dans le dashboard, avec leur date
  et un motif expurgé ;
- rétention PostgreSQL configurable des ingestions rejetées terminales sans job
  associé, fixée à 90 jours par défaut ;
- index partiel Alembic pour borner les lectures du dashboard et les lots de
  purge.

### Modifié

- le worker de rétention supprime atomiquement les événements puis l'ingestion
  rejetée, par lots internes de 100, sans contacter GLPI, Microsoft Graph ou le
  volume audio ;
- la cadence de cette purge réutilise `RETENTION_CHECK_INTERVAL_HOURS` afin de
  limiter la surface de configuration ;
- les ingestions bloquées restent exclues de la purge et aucun identifiant
  Microsoft Graph ni contenu de message n'est exposé par le dashboard.

## v0.9.0 — 2026-11-09

### Ajouté

- prise en charge des pièces jointes MP3 et M4A par le publisher, sans nouvelle
  dépendance applicative ;
- validation structurelle bornée des trames MP3 et des conteneurs M4A avant
  publication de l'audio ;
- fixtures et scénarios Microsoft Graph synthétiques pour les parcours MP3 et
  M4A valides ou corrompus.

### Modifié

- les tests d'intégration vérifient la persistance des deux nouveaux formats et
  leur normalisation effective par FFmpeg ;
- les procédures de staging, de production, de mise à jour et de rejeu
  opérateur documentent les quatre formats WAV, OGG, MP3 et M4A.

## v0.8.0 — 2026-11-09

### Ajouté

- documentation d'adoption, de contribution, de support et de signalement des
  vulnérabilités pour faciliter une évaluation interhospitalière ;
- contrôle automatique des liens relatifs de la documentation ;
- recette E2E du pipeline nominal utilisant le provider Parakeet compatible
  OpenAI.

### Modifié

- Parakeet devient le provider de transcription nominal ; le provider
  historique reste sélectionnable explicitement ;
- les données synthétiques des simulateurs ne portent plus le nom d'un
  établissement réel ;
- les limites de sécurité, de rétention et de retour arrière sont explicitées.

## v0.7.0 — 2026-11-06

- renommage complet du produit, des namespaces Python et des ressources
  d'exécution vers `mevo2glpi` ;
- nouvelle frontière cryptographique pour le numéro d'origine ;
- procédures de redéploiement depuis une stack propre.

## v0.6.0 — 2026-11-05

- adoption de la licence EUPL 1.2 ;
- ajout du numéro d'origine comme métadonnée de ticket protégée en base ;
- durcissement du web et alignement des configurations de production.

Les versions antérieures sont conservées dans les tags Git à titre historique.
