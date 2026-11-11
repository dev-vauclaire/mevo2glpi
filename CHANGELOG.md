# Journal des changements

Ce fichier recense les changements fonctionnels et opérationnels destinés aux
équipes qui évaluent ou déploient mevo2glpi. La version du produit est portée
par les tags Git immuables `vX.Y.Z` ; les versions `0.1.0` des packages Python
sont des métadonnées internes au workspace et ne constituent pas des releases.

## v0.12.0 — 2026-11-11

### Modifié

- le worker d'automatisation utilise exclusivement le contrat synchrone
  OpenAI-compatible, avec Parakeet local ou un provider externe compatible ;
- les scénarios E2E aval utilisent le simulateur OpenAI-compatible et couvrent
  le succès nominal, l'indisponibilité du provider, la reprise GLPI et la
  rétention ;
- les recettes Docker, la CI et les procédures d'exploitation sont alignées
  avec ce contrat unique.

### Supprimé

- le client Transcribe AI historique, son polling et les callbacks de
  persistance d'un identifiant distant ;
- le simulateur Transcribe AI, ses images, ses variables d'environnement et
  ses suites d'intégration spécifiques ;
- les réglages `LEGACY_TRANSCRIPTION_*`,
  `TRANSCRIPTION_POLL_INTERVAL_SECONDS` et
  `TRANSCRIPTION_JOB_TIMEOUT_SECONDS`.

### Migration et configuration requises

- la migration Alembic `0011_drop_legacy_polling` supprime les événements
  `transcription_submitted`, la colonne `transcription_provider_job_id` et
  resserre la contrainte des événements ; son downgrade restaure le schéma,
  mais pas les événements supprimés ;
- `TRANSCRIPTION_PROVIDER` n'accepte plus que `openai_compatible` et les
  variables `OPENAI_COMPATIBLE_TRANSCRIPTION_*` doivent être configurées ;
- la mise à jour doit être effectuée sans job `pending` ou `processing`, après
  validation du même déploiement en staging.

## v0.11.0 — 2026-11-10

### Ajouté

- authentification obligatoire du dashboard avec un mode exclusif `local` ou
  un fournisseur OIDC standard compatible avec Authlib ;
- comptes locaux protégés par Argon2id, verrouillage progressif, révocation des
  sessions et commandes d'administration dédiées ;
- laboratoire Keycloak local reproductible avec utilisateurs synthétiques pour
  qualifier le parcours OIDC sans donnée réelle ;
- migration additive `0010_web_authentication` créant la table `web_users` ;
- favicon cohérent avec l'identité visuelle de mevo2glpi.

### Modifié

- le web utilise un cookie applicatif signé, `Secure` et `HttpOnly`, avec
  protection CSRF et journalisation expurgée des échecs d'authentification ;
- la documentation web référence les commandes de gestion des comptes locaux,
  leurs prérequis et leur effet sur les sessions.

### Corrigé

- la requête automatique vers `/favicon.ico` ne redirige plus vers la page de
  connexion et n'invalide plus le jeton CSRF du formulaire affiché.

### Configuration requise

- `WEB_AUTH_MODE` et `WEB_AUTH_SESSION_SECRET` deviennent obligatoires ;
- le mode OIDC exige une URL d'issuer, un client confidentiel, une URI de
  redirection HTTPS exacte et une claim de rôle conforme à la configuration du
  fournisseur d'identité ;
- aucune bascule automatique n'est effectuée entre les modes `local` et `oidc`.

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
