# Procédure de mise à jour

## Objectif et périmètre

Cette procédure commence lorsqu'une révision a déjà été validée et doit être
déployée sur une installation Dokploy existante :

- un SHA précis de `main` en staging ;
- un tag annoté immuable `vX.Y.Z` en production.

La création de cette révision, la CI, la recette staging et la création du tag
sont décrites dans la [référence CI/CD](../reference/ci-cd.md). Le présent
document décrit uniquement :

1. l'arrêt contrôlé des services ;
2. l'action **Deploy** dans Dokploy ;
3. les contrôles après déploiement ;
4. le retour immédiat à la version précédente en cas d'échec.

Une migration destructive, une restauration de données, une rotation de
certificat ou une commande de rattrapage en mode `apply` nécessite un runbook
spécifique.

## Informations à relever

Avant la fenêtre de maintenance, consigner :

| Information | Staging | Production |
| --- | --- | --- |
| environnement | `staging` | `production` |
| référence cible | `main` au SHA validé | tag immuable `vX.Y.Z` |
| `APP_RELEASE` cible | `main` | `vX.Y.Z` |
| `APP_COMMIT_SHA` cible | SHA complet validé | SHA complet du tag |
| référence précédente | SHA précédemment déployé | tag précédent |

Pour un tag de production, vérifier son SHA avant toute action :

```bash
git fetch origin --tags
git rev-parse vX.Y.Z^{commit}
```

Relever également la version actuellement affichée par le dashboard ou
`GET /api/version`. Les valeurs `APP_RELEASE` et `APP_COMMIT_SHA` sont
déclaratives : elles doivent correspondre à la référence réellement
sélectionnée dans Dokploy.

## 1. Effectuer les précontrôles

Avant d'arrêter un service :

- [ ] PostgreSQL est disponible ;
- [ ] le provider de transcription est sain ;
- [ ] GLPI est joignable ;
- [ ] Microsoft Graph ou son mock est joignable par le publisher ;
- [ ] aucun worker n'est dans une boucle de redémarrage ;
- [ ] les jobs `failed` ou `blocked` connus sont consignés ;
- [ ] la référence cible et son SHA sont relevés ;
- [ ] la référence précédente et son SHA sont relevés.

Un job `failed` ou `blocked` n'empêche pas à lui seul la mise à jour. En
revanche, une dépendance indisponible ou un worker système bloqué doit être
résolu avant le déploiement.

## 2. Arrêter les services

Conserver le web actif pendant l'arrêt des workers afin de suivre leur état
dans le dashboard. Utiliser l'action d'arrêt du service dans Dokploy, qui envoie
un signal coopératif au processus.

### Ordre d'arrêt

| Ordre | Service | Objectif | Preuve d'arrêt |
| ---: | --- | --- | --- |
| 1 | `worker-publisher` | empêcher toute nouvelle ingestion Exchange | conteneur `stopped` ou `exited` dans Dokploy ; heartbeat `stopped` si l'arrêt coopératif a abouti ; aucun `current_ingestion_uuid` |
| 2 | `worker-retention` | empêcher une purge audio pendant la maintenance | conteneur `stopped` ou `exited` ; heartbeat `stopped` si disponible |
| 3 | `worker-automation` | figer transcription et création GLPI | conteneur `stopped` ou `exited` ; heartbeat `stopped` ; aucun `current_job_uuid` |
| 4 | `web` | éviter de servir une version incohérente pendant le déploiement | conteneur `stopped` ou `exited` ; endpoint `/health` indisponible |

### Vider la file avant l'arrêt de l'automatisation

Après l'arrêt du publisher, laisser `worker-automation` terminer les jobs
restants. Attendre si possible :

- zéro job `pending` ;
- zéro job `processing` ;
- zéro job `retry_scheduled`.

Le worker ne possède pas de mode drain : tant qu'il tourne, il peut réclamer le
job suivant. Comme le publisher est arrêté, la file est néanmoins finie et peut
être vidée. Si elle ne peut pas l'être pendant la fenêtre prévue, reporter la
mise à jour ou consigner explicitement les jobs qui devront reprendre.

Un arrêt coopératif pendant une transcription peut laisser le job en
`processing`. Ce comportement est prévu pour une reprise prioritaire au
redémarrage, mais doit être évité pendant une maintenance planifiée.

### Vérifier l'arrêt

L'état du conteneur dans Dokploy constitue la preuve principale. Un heartbeat
`stale` signifie seulement qu'aucun checkpoint récent n'a été écrit : il ne
prouve pas que le processus est arrêté. Si le heartbeat n'est pas passé à
`stopped`, contrôler le statut du conteneur et les derniers logs avant de
poursuivre.

Ne pas arrêter :

- la Database PostgreSQL Dokploy ;
- le moteur de transcription, sauf s'il est lui-même mis à jour ;
- `database-migrations`, qui est un service one-shot normalement déjà en état
  `exited (0)`.

### Point de reprise

Une fois les services mutateurs arrêtés, réaliser le point de reprise
PostgreSQL et volume audio prévu par la politique d'exploitation. Les deux
sauvegardes doivent représenter le même état logique.

Le dépôt ne contient pas encore de méthode de sauvegarde et de restauration
qualifiée pour le stockage choisi. En l'absence d'un runbook externe testé,
seul un rollback applicatif sans restauration de données peut être garanti.

## 3. Préparer Dokploy

Dans chaque projet concerné :

1. sélectionner la référence Git cible ;
2. vérifier le SHA extrait par Dokploy ;
3. ne modifier aucun secret ni aucune autre variable sans nécessité ;
4. enregistrer la configuration avant de lancer le déploiement.

Dans **Transcription** et **Applications (mevo2glpi)**, renseigner également :

- `APP_RELEASE` avec `main` ou le tag ;
- `APP_COMMIT_SHA` avec le SHA complet.

`APP_ENVIRONMENT` appartient uniquement au projet **Applications (mevo2glpi)**. Le
projet staging **Providers mocks** ne consomme aucune de ces trois variables :
contrôler sa révision directement dans Dokploy.

En production, ne jamais sélectionner `main`, une branche de fonctionnalité ou
un alias mutable comme `latest`.

## 4. Déployer la transcription si elle est concernée

Cette étape est nécessaire uniquement si la release modifie l'API, le runtime,
le modèle ou l'image de transcription.

Dans le projet Dokploy **Transcription** :

1. sélectionner la même référence et le même SHA que la release applicative ;
2. mettre à jour `APP_RELEASE` et `APP_COMMIT_SHA` ;
3. cliquer sur **Deploy** ;
4. attendre la fin du build et le démarrage du service ;
5. vérifier que le conteneur est `running` et `healthy` ;
6. vérifier que `GET /health/ready` répond avec succès.

La readiness confirme que le modèle est chargé. Ne pas déployer les
applications si cette étape échoue.

## 5. Déployer les applications

Dans le projet Dokploy **Applications (mevo2glpi)** :

1. vérifier une dernière fois la référence, `APP_RELEASE` et
   `APP_COMMIT_SHA` ;
2. cliquer une seule fois sur **Deploy** ;
3. suivre les logs du build et de `database-migrations` ;
4. attendre que Dokploy termine le déploiement.

Le Compose exécute automatiquement la séquence suivante :

```text
database-migrations -> web + worker-publisher + worker-automation + worker-retention
```

`database-migrations` doit terminer avec le code `0`. Les quatre services
persistants ne démarrent qu'après cette réussite. Il n'existe aucune pause
d'approbation entre la migration et leur démarrage : il ne faut pas relancer
manuellement les workers après avoir cliqué sur **Deploy**.

## 6. Vérifier la mise à jour

### Dans Dokploy

- [ ] le déploiement est terminé sans erreur ;
- [ ] `database-migrations` est en état `exited (0)` ;
- [ ] `web` est `running` et `healthy` ;
- [ ] les trois workers sont `running` et `healthy` ;
- [ ] aucun conteneur ne redémarre en boucle ;
- [ ] Transcription est `running` et `healthy` si elle a été redéployée.

L'état `exited (0)` de `database-migrations` est nominal : ce service ne doit
pas rester `running`.

### Dans l'application

- [ ] `GET /health` répond avec le code `200` ;
- [ ] le dashboard ou `GET /api/version` affiche l'environnement, la release et
      le SHA attendus ;
- [ ] les trois heartbeats sont récents ;
- [ ] les trois runtimes sont revenus à l'état `running` ;
- [ ] aucun worker ne conserve un UUID courant inattendu ;
- [ ] aucun job ne reste anormalement bloqué par la mise à jour.

Un conteneur worker peut être `healthy` avec un runtime `degraded` ou `blocked`.
Le statut Docker ne suffit donc pas : vérifier également le dashboard et les
logs.

### Dépendances externes

Les healthchecks des workers valident PostgreSQL, les heartbeats et l'accès au
volume audio. Ils ne prouvent pas le fonctionnement de Microsoft Graph, GLPI ou
du provider de transcription.

Contrôler les logs expurgés :

- [ ] le publisher effectue une découverte Graph sans erreur `401`, `403` ou
      `transport_error` ;
- [ ] l'automatisation ne signale aucune erreur TLS, transcription ou GLPI ;
- [ ] la rétention ne signale aucune erreur TLS ou GLPI ;
- [ ] aucun secret, contenu audio ou transcription n'est journalisé.

Terminer par un traitement synthétique autorisé de bout en bout. En staging,
utiliser les fixtures WAV et OGG. En production, n'utiliser aucune donnée réelle
de patient et respecter la fenêtre de recette validée par les opérateurs GLPI.

La mise à jour est validée uniquement lorsque l'identité de version, les états
runtime et le traitement synthétique sont tous conformes.

## 7. Échec et rollback immédiat

En cas d'échec, arrêter de nouveau les services dans l'ordre suivant :

```text
worker-publisher -> worker-retention -> worker-automation -> web
```

### Échec avant le clic sur Deploy

Si aucune migration ni nouvelle image n'a été lancée, restaurer la référence et
les variables précédentes, puis redéployer la version précédente.

### Rollback applicatif

Si le schéma reste compatible avec la version précédente :

1. sélectionner la référence précédente ;
2. restaurer son `APP_RELEASE` et son `APP_COMMIT_SHA` ;
3. redéployer d'abord Transcription si elle doit également revenir en arrière ;
4. cliquer sur **Deploy** dans Applications (mevo2glpi) ;
5. répéter intégralement les contrôles de cette procédure.

Ne jamais exécuter automatiquement un `alembic downgrade`. Après la réussite de
la migration, le Compose redémarre automatiquement le web et les workers ; le
rollback doit donc être préparé avant le clic sur **Deploy**.

### Restauration de données

Si PostgreSQL ou le volume audio doit être restauré, ne pas utiliser ce rollback
court. Maintenir les services arrêtés et suivre un runbook de restauration et de
réconciliation dédié. Une restauration locale n'annule ni un message déplacé
dans Exchange, ni un ticket déjà créé dans GLPI.
