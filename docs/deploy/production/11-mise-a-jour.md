# 11. Mettre à jour une installation existante

Cette procédure commence lorsqu'un nouveau tag annoté immuable est prêt. Elle
ne couvre ni une migration destructive, ni une rotation de certificat, ni une
commande de rattrapage en mode `apply`, qui nécessitent un runbook dédié.

## 1. Consigner les références

Relever :

| Information | Valeur |
| --- | --- |
| tag et SHA actuellement déployés | version précédente |
| tag et SHA cibles | nouvelle version |
| révision Alembic actuelle | valeur avant maintenance |
| révision Alembic cible | valeur de la release |

```bash
git fetch origin --tags
git rev-parse 'vX.Y.Z^{commit}'
```

Les valeurs `APP_RELEASE` et `APP_COMMIT_SHA` sont déclaratives : elles doivent
correspondre à la référence réellement sélectionnée dans Dokploy.

## 2. Précontrôles

- [ ] PostgreSQL, la transcription, GLPI, Entra ID et Graph sont disponibles ;
- [ ] aucun worker ne redémarre en boucle ;
- [ ] les jobs `failed` ou `blocked` connus sont consignés ;
- [ ] les tags et SHA précédent et cible sont relevés ;
- [ ] le mécanisme de point de reprise de l'hyperviseur est disponible si la
  politique de l'établissement l'exige.

Une dépendance indisponible ou un worker système bloqué doit être corrigé avant
la maintenance.

## 3. Arrêter et vider le pipeline

Conserver le web actif le temps de suivre la file. Arrêter dans Dokploy :

| Ordre | Service | Preuve attendue |
| ---: | --- | --- |
| 1 | `worker-publisher` | conteneur `stopped`/`exited`, aucune ingestion courante |
| 2 | `worker-retention` | conteneur `stopped`/`exited` |
| 3 | `worker-automation` | conteneur `stopped`/`exited`, aucun job courant |
| 4 | `web` | conteneur arrêté, `/health` indisponible |

Après l'arrêt du publisher et avant celui de l'automatisation, laisser la file
finie se vider. Attendre si possible zéro job `pending`, `processing` et
`retry_scheduled`. Le worker ne possède pas de mode drain ; reporter la mise à
jour si l'état ne peut pas être stabilisé.

L'état du conteneur est la preuve principale. Un heartbeat `stale` ne prouve
pas l'arrêt. Ne pas arrêter PostgreSQL ni la transcription, sauf si cette
dernière est elle-même mise à jour.

Une fois les services mutateurs arrêtés, déclencher le point de reprise prévu
par l'hyperviseur. Le dépôt n'automatise pas la sauvegarde de PostgreSQL, du
volume audio ou des secrets.

## 4. Préparer Dokploy

Dans chaque projet concerné :

1. sélectionner le tag cible ;
2. vérifier le SHA extrait par Dokploy ;
3. mettre à jour `APP_RELEASE` et `APP_COMMIT_SHA` ;
4. ne modifier aucun autre secret ou paramètre sans nécessité ;
5. conserver l'auto-déploiement désactivé.

## 5. Déployer

Si la release modifie l'API, le runtime, le modèle ou l'image de transcription,
déployer d'abord le projet **Transcription** et attendre son état `healthy`.

Dans **Applications mevo2glpi**, cliquer une seule fois sur **Deploy**. Le
Compose exécute la migration puis démarre automatiquement le web et les trois
workers :

```text
database-migrations -> web + worker-publisher + worker-automation + worker-retention
```

`database-migrations` doit terminer en `exited (0)`. Il n'existe aucune pause
d'approbation entre la migration et le redémarrage des services persistants.

## 6. Valider

Répéter les contrôles de [validation](10-validation.md), notamment :

- tag, SHA et environnement affichés ;
- migrations réussies ;
- conteneurs et runtimes opérationnels ;
- dépendances externes accessibles ;
- traitement synthétique unique de bout en bout.

La mise à jour n'est terminée qu'après validation fonctionnelle.

## Compatibilité historique avant `v0.12.0`

La migration `0011_drop_legacy_polling` retire la colonne
`transcription_provider_job_id` et les événements `transcription_submitted`.
Lors d'un passage direct depuis une version antérieure à `v0.12.0`, vérifier
qu'aucun job n'est en cours et préparer un point de reprise PostgreSQL si ces
données historiques doivent être conservées. Un downgrade du schéma ne recrée
pas les données supprimées.

En cas d'échec, suivre immédiatement
[rollback et diagnostic](12-rollback-et-diagnostic.md).
