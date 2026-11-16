# 1. Vérifier les prérequis

## Système et dimensionnement

Le système qualifié est **Ubuntu Server 24.04 LTS**. Une autre distribution
Linux prise en charge par Dokploy doit être qualifiée séparément.

Lorsque Parakeet et PostgreSQL tournent sur la même VM, prévoir au minimum :

- 4 vCPU ;
- 12 Gio de RAM ;
- 50 Gio de disque.

Ce minimum ne garantit pas la capacité : mesurer l'occupation du disque, la
mémoire, la charge CPU et la durée des transcriptions après mise en service.
Avec une transcription externe, dimensionner la VM d'après la charge des
applications et de PostgreSQL ; ce dépôt ne qualifie pas de minimum inférieur.

## Informations et responsabilités à réunir

### Version applicative

- tag annoté immuable `vX.Y.Z` ;
- SHA Git complet correspondant ;
- tag et SHA de la version précédente ;
- fenêtre de déploiement et responsables du contrôle fonctionnel.

Vérifier localement la cible :

```bash
git fetch origin --tags
git rev-parse 'vX.Y.Z^{commit}'
```

### Services externes

- nom DNS public du dashboard et certificat serveur associé ;
- tenant Entra ID, application single-tenant et boîte Exchange dédiée ;
- Keycloak externe exposant OpenID Connect et client confidentiel dédié ;
- GLPI 11.0.7 avec API V2.3.0, compte technique, client OAuth et source `MEVO` ;
- choix explicite entre Parakeet local et provider OpenAI-compatible externe.

### Accès d'administration

- administration SSH de la VM par clé et depuis un réseau restreint ;
- accès Dokploy réservé aux administrateurs ;
- droits Entra/Exchange nécessaires à l'Application RBAC ;
- droits GLPI minimaux nécessaires à la création et à la consultation des
  tickets ;
- mécanisme de sauvegarde de la VM défini par l'établissement.

## Installer Dokploy

La version qualifiée est **Dokploy 0.30.10**, installée avec le script Docker
publié pour cette version :

```bash
curl --fail --show-error --location \
  https://github.com/Dokploy/dokploy/releases/download/v0.30.10/install.sh \
  | sudo sh
```

Avant de poursuivre, vérifier que Dokploy est accessible sur le port
d'administration prévu et que Docker redémarre correctement après un reboot de
la VM.

## Porte de validation

- [ ] la VM satisfait le profil retenu ;
- [ ] le tag et son SHA sont consignés ;
- [ ] chaque service externe possède un responsable identifié ;
- [ ] les noms DNS, certificats et règles réseau sont disponibles ;
- [ ] la stratégie de transcription est choisie ;
- [ ] Dokploy 0.30.10 est opérationnel.

Étape suivante : [réseau, DNS et TLS](02-reseau-dns-et-tls.md).
