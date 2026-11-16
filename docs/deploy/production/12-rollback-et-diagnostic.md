# 12. Revenir en arrière et diagnostiquer

## Mesures immédiates

En cas d'échec de déploiement ou de comportement inattendu, arrêter les
services mutateurs :

```text
worker-publisher -> worker-retention -> worker-automation -> web
```

Ne supprimer ni volume, ni conteneur PostgreSQL, ni fichier de secret. Conserver
les logs expurgés, le tag, le SHA, la révision Alembic et les UUID techniques
nécessaires au diagnostic.

## Choisir le rollback adapté

### Aucun déploiement n'a commencé

Restaurer la référence et les variables précédentes dans Dokploy. Aucun état
applicatif n'a été modifié.

### Rollback applicatif avec schéma compatible

1. sélectionner le tag précédent ;
2. restaurer son `APP_RELEASE` et son `APP_COMMIT_SHA` ;
3. redéployer d'abord la transcription si elle doit revenir en arrière ;
4. déployer ensuite les applications ;
5. répéter intégralement la validation technique et fonctionnelle.

Ne jamais exécuter automatiquement `alembic downgrade`.

### Restauration de données nécessaire

Maintenir les services arrêtés et appliquer le runbook de l'hyperviseur. La
restauration de PostgreSQL, du volume audio et des secrets doit porter sur un
point cohérent. Elle exige ensuite une réconciliation : restaurer la VM
n'annule ni un message déplacé dans Exchange, ni un ticket déjà créé dans GLPI.

## Diagnostic ciblé

| Symptôme | Contrôles prioritaires |
| --- | --- |
| migration ne joint pas PostgreSQL | réseau Database, nom DNS réel, URL encodée, état de la Database |
| publisher `401` ou `403` | certificat et clé associés, Tenant/Client/Mailbox ID, propagation et scope Exchange RBAC |
| publisher `transport_error` | DNS, port 443, bundle CA, horloge système |
| connexion OIDC expirée/refusée | URL publique HTTPS, redirect URI exacte, issuer, cookie Secure, rôle et claim |
| transcription indisponible | health/readiness, alias et réseau, API key, timeout et ressources CPU/RAM |
| GLPI `transport_error` | DNS, chaîne TLS, URL directe sans portail interactif |
| GLPI `400 invalid_grant` | compte technique, mot de passe et client OAuth GLPI |
| worker `degraded` après reprise | logs récents, heartbeat et succès d'un nouveau cycle ; le dernier incident peut rester visible jusqu'au cycle sain |
| conteneur redémarre en boucle | première erreur de démarrage, bind mounts, permissions `root:10001`, variables obligatoires |
| audio introuvable | nom du volume, mounts et existence du fichier canonique ; ne pas créer un volume de remplacement |

Commandes de contrôle non destructives :

```bash
sudo docker network inspect mevo2glpi-prod-database-network
sudo docker network inspect mevo2glpi-prod-transcription-network
sudo stat -c '%U %G %a %n' /etc/mevo2glpi/secrets/*
```

Pour TLS, reprendre les commandes de
[réseau, DNS et TLS](02-reseau-dns-et-tls.md). Ne jamais afficher une variable
d'environnement contenant un secret dans un ticket de support.

## Limites du rollback

- un ticket GLPI déjà créé reste la source de vérité métier ;
- un message Exchange déplacé n'est pas automatiquement replacé ;
- une transcription externe déjà demandée ne peut pas toujours être annulée ;
- un downgrade Alembic peut être incompatible ou perdre des données ;
- sans point de reprise testé, seul le rollback applicatif compatible est
  garanti.

Après correction, redémarrer par la [procédure de déploiement](09-deploiement.md)
et refaire la [validation](10-validation.md).
