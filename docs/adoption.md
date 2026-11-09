# Guide d'adoption

Ce document aide un établissement à évaluer mevo2glpi avant toute intégration.
Le dépôt fournit une base fonctionnelle déjà exploitée dans une topologie
hospitalière précise. Il ne constitue pas une solution prête à déployer sans
qualification locale.

## Cas d'usage

mevo2glpi récupère les messages vocaux d'une boîte Exchange Online dédiée,
valide leur pièce jointe audio, la transcrit, crée un ticket GLPI puis conserve
l'audio jusqu'à la clôture du ticket ou son échéance absolue.

Les tests, simulateurs et exemples utilisent exclusivement des données
synthétiques. Aucune donnée réelle de patient ne doit être utilisée pendant
l'évaluation.

## Compatibilité qualifiée

| Composant | Configuration qualifiée | Portabilité attendue |
| --- | --- | --- |
| GLPI | GLPI 11.0.7, API V2.3.0, OAuth Password | Toute autre version ou méthode d'authentification doit être requalifiée |
| Messagerie | Exchange Online et Microsoft Graph, application Entra ID authentifiée par certificat et limitée à une boîte dédiée | Un autre système de messagerie nécessite un nouvel adaptateur |
| Audio entrant | Une unique pièce jointe visible WAV, OGG, MP3 ou M4A par message, limitée à 25 Mio | Les autres formats et plusieurs pièces jointes visibles ne sont pas pris en charge |
| Transcription nominale | API locale compatible OpenAI, Parakeet Ultra sur CPU | Le contrat permet un autre provider compatible ou l'adaptateur historique |
| Persistance | PostgreSQL | Le schéma est géré par Alembic |
| Déploiement de référence | Ubuntu Server 24.04 LTS, Docker et Dokploy | Dokploy est la topologie testée, pas une dépendance du code métier |

La version réellement déployée est identifiée par le tag Git, le SHA et
`APP_ENVIRONMENT`. Les versions `0.1.0` des packages Python sont internes au
workspace et ne représentent pas la version du produit.

## Contrats à adapter à l'établissement

Avant un essai connecté, vérifier explicitement :

- le format de l'objet généré par l'autocommutateur ; l'extraction actuelle du
  numéro d'origine cible un objet français de la forme « Message vocale de la
  part de … pour … » ;
- le format des numéros ; l'implémentation actuelle reconnaît les numéros
  français à dix chiffres et les considère comme un numéro d'origine, pas
  nécessairement comme le numéro de rappel demandé ;
- la présence d'une seule pièce jointe WAV, OGG, MP3 ou M4A non vide,
  inférieure ou égale à 25 Mio et exploitable ;
- l'identifiant de la source GLPI locale, le type de ticket et les autres
  valeurs fixes attendues par l'établissement ;
- le comportement de l'API GLPI, les droits du compte technique et la création
  du client OAuth ;
- le fuseau fonctionnel, actuellement `Europe/Paris` ;
- la qualité et les performances du provider de transcription avec des audios
  fictifs représentatifs de la téléphonie locale.

## Topologie de référence

Dans la topologie documentée, PostgreSQL est externe au Compose applicatif mais
reste déployé sur la même VM comme service **Database Dokploy**. La
transcription locale possède son propre projet Compose. Exchange Online et
GLPI restent des services externes.

Ces choix peuvent évoluer si les contrats réseau, les volumes persistants, les
healthchecks et l'ordre des migrations restent respectés.

## Parcours d'évaluation recommandé

1. Cloner un tag Git immuable et lire la
   [référence d'architecture](reference/architecture.md).
2. Exécuter les tests déterministes et lancer les services de test locaux selon
   la [procédure de développement](procedures/developpement-local.md).
3. Utiliser le simulateur Microsoft Graph et uniquement des audios synthétiques
   pour vérifier les acceptations et rejets.
4. Déployer le même commit en staging selon la
   [procédure dédiée](procedures/staging.md).
5. Raccorder un GLPI de test et qualifier le client, les champs créés, la
   rétention et les droits.
6. Faire valider localement l'application Entra ID, son certificat, son rôle
   Exchange et sa restriction à la boîte dédiée.
7. Réaliser une analyse de risques, une revue RGPD et une recette fonctionnelle
   avant d'envisager la production.

## Sécurité et exploitation à qualifier localement

Avant de raccorder mevo2glpi à son système d'information, l'établissement
adoptant doit intégrer les contrôles suivants à sa propre analyse de risques et
à ses procédures d'exploitation :

- limiter le dashboard à un réseau de confiance, car l'authentification et les
  autorisations applicatives ne sont pas encore implémentées ; l'intégration
  OIDC avec Keycloak reste une évolution future ;
- sélectionner un tag Git immuable, auditer son code et appliquer les outils
  internes de contrôle de la chaîne logicielle, notamment la détection de
  secrets, l'analyse des dépendances et l'analyse des images conteneur ;
- injecter les secrets et certificats avec les mécanismes approuvés localement,
  sans les ajouter au dépôt ni aux images, et protéger les clés privées avec
  des permissions minimales ;
- restreindre l'application Entra ID à la seule boîte Exchange dédiée avec
  Exchange Application RBAC ;
- conserver la vérification TLS et installer les autorités internes dans les
  magasins de confiance gérés par l'exploitation ;
- couvrir PostgreSQL et le volume audio par la politique de sauvegarde de
  l'hyperviseur, puis qualifier la fréquence, la rétention, le chiffrement et
  la restauration de ces sauvegardes ; le dépôt ne les automatise pas ;
- appliquer les mises à jour uniquement lorsque les jobs `pending` et
  `processing` ont été vidés : un rollback applicatif ne peut annuler ni un
  message déjà déplacé dans Exchange, ni un ticket déjà créé dans GLPI.

## Limites connues

- authentification et autorisation du dashboard non implémentées ;
- parcours E2E nominal Parakeet automatisé mais volontairement lancé hors CI,
  car il construit et charge le modèle réel ;
- la validation du publisher reste structurelle : le décodage effectif relève
  de FFmpeg dans le provider de transcription ;
- suite historique Transcribe AI dépendante d'images qui peuvent ne pas être
  accessibles à un autre établissement ;
- aucune garantie de compatibilité GLPI au-delà de la version qualifiée ;
- aucune restauration croisée de PostgreSQL, du volume, d'Exchange et de GLPI
  n'est fournie par l'application.

## Licence et contributions

Le projet est distribué sous EUPL 1.2. Toute redistribution doit conserver
[`LICENSE`](../LICENSE), [`NOTICE`](../NOTICE) et les avis de composants tiers.
Les modalités de contribution, de sécurité et de support sont décrites dans
[`CONTRIBUTING.md`](../CONTRIBUTING.md), [`SECURITY.md`](../SECURITY.md) et
[`SUPPORT.md`](../SUPPORT.md).
