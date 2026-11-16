# 9. Déployer les services

Le déploiement initial doit être manuel. Avant de commencer, vérifier que
l'Inbox dédiée ne contient que les messages que le publisher est autorisé à
consommer : dès son démarrage, il traite le backlog du plus ancien au plus
récent.

## 1. Démarrer PostgreSQL

Dans Dokploy :

1. démarrer la ressource Database ;
2. vérifier son état `running` ;
3. vérifier qu'elle est attachée à
   `mevo2glpi-prod-database-network` ;
4. confirmer que son nom DNS correspond à celui de `DATABASE_URL`.

PostgreSQL ne possède ni domaine Traefik ni port publié.

## 2. Déployer la transcription locale, si retenue

Dans le projet **Transcription** :

1. contrôler le tag, `APP_RELEASE` et `APP_COMMIT_SHA` ;
2. cliquer une seule fois sur **Deploy** ;
3. attendre le téléchargement et la vérification du modèle ;
4. attendre que `transcription-api` soit `running` et `healthy`.

La readiness `/health/ready` confirme que le modèle est chargé. Ne pas déployer
les applications tant que ce contrôle échoue.

Avec un provider externe, vérifier sa disponibilité et son contrat depuis la
VM, puis passer directement à l'étape suivante.

## 3. Déployer mevo2glpi

Dans le projet **Applications mevo2glpi** :

1. contrôler le tag, `APP_RELEASE`, `APP_COMMIT_SHA` et `APP_ENVIRONMENT` ;
2. cliquer une seule fois sur **Deploy** ;
3. suivre les logs de build puis ceux de `database-migrations` ;
4. attendre la fin du déploiement Dokploy.

Le Compose impose la séquence :

```text
database-migrations -> web + worker-publisher + worker-automation + worker-retention
```

`database-migrations` doit terminer en `exited (0)`. Cet état est nominal pour
un service one-shot. Les quatre services persistants utilisent
`restart: unless-stopped` et redémarrent automatiquement après un reboot de la
VM, sous réserve que Docker et Dokploy soient eux-mêmes opérationnels.

## Contrôle immédiat

- [ ] `database-migrations` est `exited (0)` ;
- [ ] le web et les trois workers sont `running` et `healthy` ;
- [ ] aucun conteneur ne redémarre en boucle ;
- [ ] la transcription locale est `healthy`, si elle est déployée ;
- [ ] le dashboard présente le tag et le SHA attendus ;
- [ ] les logs ne contiennent ni secret, ni transcription, ni contenu audio.

Un conteneur worker peut être `healthy` alors que son runtime est `degraded` ou
`blocked`. Le statut Docker ne remplace pas le contrôle du dashboard et des
logs.

En cas d'échec, ne pas relancer plusieurs fois le déploiement. Conserver les
logs expurgés et suivre [rollback et diagnostic](12-rollback-et-diagnostic.md).

Étape suivante : [validation fonctionnelle](10-validation.md).
