# Procédure de mise à jour

## Objectif et périmètre

Cette procédure encadre la promotion d'une version déjà validée par la CI vers
staging puis production. Elle couvre l'identité Git, les sauvegardes, la
migration one-shot, la remise en service, la validation et le rollback.

Elle ne remplace pas la procédure de première installation de chaque
environnement. Une commande de maintenance ou de rattrapage en mode `apply`
nécessite une procédure et une validation distinctes.

## Prérequis

- la Pull Request a été relue et tous ses contrôles CI sont verts ;
- `main` contient uniquement les commits attendus ;
- la compatibilité de la migration avec la version précédente est documentée ;
- le staging dispose de données synthétiques et de services de test isolés ;
- la restauration PostgreSQL et audio a déjà été testée ;
- la fenêtre de maintenance et le responsable de la décision sont identifiés.

Les migrations doivent rester additives et compatibles avec la version
précédente. Une migration destructive exige une procédure spécifique et ne doit
pas suivre automatiquement ce flux.

## Identifier la révision

Pour staging, relever le SHA complet de `origin/main`. Pour production, utiliser
un tag annoté immuable `vX.Y.Z` créé sur le SHA validé en staging.

```bash
git fetch origin --tags
git rev-parse origin/main^{commit}
git rev-list -n 1 vX.Y.Z
```

Avant le déploiement, renseigner :

- `APP_ENVIRONMENT` avec l'environnement cible ;
- `APP_RELEASE` avec `main` en staging ou le tag en production ;
- `APP_COMMIT_SHA` avec le SHA complet réellement sélectionné.

Dokploy reconstruit actuellement les images depuis le dépôt. Le tag fournit une
référence d'audit, mais l'opérateur doit encore comparer le SHA cloné par
Dokploy avec `APP_COMMIT_SHA`.

## Préparer une mise à jour

1. Consigner la release, le SHA et la révision Alembic actuellement déployés.
2. Vérifier dans le dashboard qu'aucun traitement n'est dans une situation
   ambiguë nécessitant une décision opérateur.
3. Arrêter les trois workers afin de figer les mutations PostgreSQL, audio,
   Exchange et GLPI. Le web peut rester en lecture seule pendant la sauvegarde.
4. Sauvegarder PostgreSQL et le volume audio dans cet état cohérent.
5. Identifier les sauvegardes avec l'environnement, la date UTC, le tag et le
   SHA de la version encore en place.
6. Vérifier que les sauvegardes sont terminées et restaurables avant toute
   migration.

PostgreSQL et le volume audio forment un même point de reprise. Ils ne doivent
pas être restaurés depuis deux instants différents.

## Déployer en staging

1. Déployer le SHA de `main` retenu dans le projet Dokploy staging.
2. Laisser Compose exécuter `database-migrations`. Le web et les trois workers
   démarrent automatiquement uniquement si ce service termine avec le code
   `0`.
3. Vérifier la révision Alembic atteinte et l'absence d'erreur PostgreSQL.
4. Vérifier sur le dashboard `environment`, `release` et le SHA court.
5. Contrôler `/health`, les heartbeats et l'absence de retry anormal.
6. Exécuter la recette synthétique de la
   [procédure staging](staging.md).
7. Consigner le résultat et le SHA effectivement testé.

Une modification après cette recette invalide la promotion : le nouveau SHA
doit repasser par la CI et le staging.

## Créer la release

Après validation du staging, geler temporairement les fusions dans `main`, puis
créer le tag sur le SHA exactement recetté :

```bash
git switch main
git pull --ff-only
git status --short
GTA_RELEASE_SHA=remplacer_par_le_sha_valide_en_staging
git tag -a vX.Y.Z -m "release: vX.Y.Z" "$GTA_RELEASE_SHA"
git push origin vX.Y.Z
git rev-list -n 1 vX.Y.Z
```

Le tag ne doit jamais être déplacé ni réutilisé. Vérifier que son SHA correspond
toujours à celui relevé sur staging avant de poursuivre.

## Déployer en production

1. Appliquer la préparation et les sauvegardes décrites ci-dessus.
2. Configurer Dokploy sur le tag immuable et son SHA complet.
3. Déployer le projet applicatif. Compose exécute d'abord
   `database-migrations` et empêche le démarrage des runtimes en cas d'échec.
4. Vérifier le code `0` de la migration et la révision Alembic atteinte.
5. Contrôler l'endpoint `/health` du web et les heartbeats des workers démarrés
   après la migration.
6. Contrôler les accès de chaque worker sans afficher de secret : Graph pour le
   publisher, transcription et GLPI pour l'automatisation, GLPI pour la
   rétention.
7. Observer au moins un traitement représentatif autorisé par la fenêtre de
   recette, puis décider du maintien de la version.

La production n'est jamais déclenchée automatiquement à la suite d'un push ou
de la création d'un tag. Le manifeste actuel ne prévoit pas de validation
humaine entre la réussite de la migration et le démarrage des runtimes ; ajouter
ce point de contrôle nécessiterait de séparer la migration dans un projet
Dokploy distinct.

## Contrôles après déploiement

- la migration one-shot est terminée avec le code `0` ;
- le dashboard affiche l'environnement, le tag et le SHA attendus ;
- le web et les trois workers sont sains et leurs heartbeats sont récents ;
- les journaux ne montrent ni boucle de retry, ni erreur TLS, ni erreur
  d'authentification ;
- aucun secret, contenu audio ou transcription n'est journalisé ;
- le flux testé ne crée qu'un seul ticket et conserve l'audio selon la politique
  attendue.

## Décider un rollback

Arrêter immédiatement les workers avant l'analyse si :

- la migration échoue ;
- l'identité de version ne correspond pas au SHA attendu ;
- un worker produit une boucle ou des effets distants inattendus ;
- les healthchecks ou dépendances essentielles restent indisponibles ;
- une incohérence PostgreSQL ou audio est détectée.

### Aucun effet métier depuis la sauvegarde

Si aucun message Exchange, ticket GLPI ni audio n'a été modifié, restaurer le
couple PostgreSQL/volume audio si nécessaire, puis redéployer le tag précédent
compatible avec la révision restaurée.

### Des effets métier ont déjà eu lieu

Une restauration locale n'annule ni un déplacement Exchange ni la création
d'un ticket GLPI. Dans ce cas :

1. maintenir les workers arrêtés ;
2. consigner les UUID, tickets et messages concernés sans copier de donnée
   sensible dans les logs ou le rapport ;
3. comparer PostgreSQL, le volume audio, Exchange et GLPI ;
4. décider explicitement des effets distants à conserver ou réconcilier ;
5. restaurer PostgreSQL et l'audio au même point uniquement après cette
   décision ;
6. redéployer le tag précédent compatible ;
7. vérifier la réconciliation avant de relancer le publisher.

Ne jamais relancer aveuglément les workers après une restauration : un état
distant déjà produit pourrait être dupliqué.

## Traçabilité attendue

Conserver pour chaque mise à jour :

- l'environnement, le tag et le SHA ;
- les identifiants des sauvegardes PostgreSQL et audio ;
- les révisions Alembic avant et après migration ;
- l'heure UTC du début et de la fin ;
- le résultat de la recette et la décision de maintien ou de rollback ;
- l'identité de l'opérateur, sans secret ni donnée sensible.
