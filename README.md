<p align="center">
  <picture>
    <source
      media="(prefers-color-scheme: dark)"
      srcset="assets/logo_circulaire_sombre.png"
    >
    <source
      media="(prefers-color-scheme: light)"
      srcset="assets/logo_circulaire_claire.png"
    >
    <img
      src="assets/logo_circulaire_claire.png"
      alt="Logo mevo2glpi"
      width="320"
    >
  </picture>
</p>

<p align="center">
  Automatise la transformation des messages vocaux reçus via Exchange Online
  en tickets GLPI transcrits et traçables.
</p>

## Sommaire

- [Sommaire](#sommaire)
- [Fonctionnalités principales](#fonctionnalités-principales)
- [Architecture](#architecture)
  - [mevo2glpi](#mevo2glpi)
  - [Services connectés](#services-connectés)
- [Procédures par environnement](#procédures-par-environnement)
- [Documentation](#documentation)
- [Feuille de route](#feuille-de-route)
  - [Évolutions prioritaires](#évolutions-prioritaires)
  - [Sécurité de l'interface web](#sécurité-de-linterface-web)
  - [Génération assistée du ticket par LLM](#génération-assistée-du-ticket-par-llm)
- [Licence](#licence)

## Fonctionnalités principales

- récupération et filtrage des pièces jointes d'une boîte Exchange Online via
  Microsoft Graph, avec des identifiants immuables ;
- orchestration du pipeline par une base de données PostgreSQL : jobs,
  événements, tentatives, rétentions et heartbeats ;
- transcription des pièces jointes par un provider interchangeable :
  Transcribe AI ou un service compatible OpenAI ;
- API locale optionnelle sur CPU avec Parakeet Ultra, `transcribe.cpp` et
  normalisation FFmpeg ;
- création de tickets GLPI V2.3.0 contenant la transcription et un accès à
  l'audio ;
- suppression crash-safe des audios à la clôture du ticket ou à une échéance
  configurable ;
- dashboard des jobs, des événements, de l'état des workers et des alertes,
  avec relance par l'opérateur.

## Architecture

```mermaid
%%{init: {"flowchart": {"nodeSpacing": 70, "rankSpacing": 90}}}%%
flowchart TB
    USER["Opérateur"]

    subgraph CENTER_ROW[" "]
        direction LR
        EX["Microsoft Exchange Online<br/>messages vocaux"] -->|fournit les audios| APP["mevo2glpi"]
        APP <-->|crée et suit les tickets| GLPI["GLPI"]
    end

    TRANSCRIPTION["Transcription<br/>locale ou externe"]

    USER -->|consulte et relance| APP
    APP <-->|demande et reçoit la transcription| TRANSCRIPTION

    style CENTER_ROW fill:transparent,stroke:transparent
```

La [référence d'architecture détaillée](docs/reference/architecture.md)
présente les services internes, la persistance et le volume audio.

### mevo2glpi

L'application orchestre l'ingestion, la transcription, la création des tickets
et leur suivi. Son dashboard permet à l'opérateur de consulter les traitements
et de relancer ceux qui ne nécessitent pas de correction manuelle.

### Services connectés

- **Exchange Online** fournit les messages vocaux et leurs pièces jointes ;
- le **provider de transcription**, local ou externe, transforme les audios en
  texte ;
- **GLPI** reçoit les tickets et expose leur état jusqu'à leur clôture.

## Procédures par environnement

Les déploiements en staging et en production s'effectuent avec **Dokploy**.

- [développement local](docs/procedures/developpement-local.md) ;
- [staging](docs/procedures/staging.md) ;
- [production](docs/procedures/production.md) ;
- [mise à jour](docs/procedures/mise-a-jour.md).

## Documentation

- [architecture technique détaillée](docs/reference/architecture.md) ;
- [architecture du dépôt et workspace uv](docs/reference/depot.md) ;
- [cycle CI/CD et convention des tags](docs/reference/ci-cd.md) ;
- [stratégie de tests](docs/reference/tests.md).

## Feuille de route

Cette feuille de route contient uniquement les évolutions prévues. Les
fonctionnalités déjà disponibles sont décrites dans les sections précédentes.

### Évolutions prioritaires

- [ ] Générer l'UUID d'ingestion de manière déterministe à partir de l'identité
  immuable du message Exchange afin de renforcer l'idempotence et les reprises
  après rollback.
- [ ] Mettre en œuvre la
  [rétention de l'historique PostgreSQL](docs/conception/retention-postgresql.md)
  selon la politique validée.
- [ ] Étendre l'ingestion Exchange aux formats MP3 et M4A après validation de
  leur structure et de leur compatibilité avec le service de transcription.

### Sécurité de l'interface web

- [ ] Définir avec les équipes réseau et système le mécanisme SSO placé devant
  l'application.
- [ ] Ajouter une authentification et des autorisations applicatives avant
  d'étendre l'accès aux audios ou les actions opérateur.

En attendant, le web reste strictement limité au réseau interne par le DNS, le
pare-feu et la configuration Traefik de Dokploy.

### Génération assistée du ticket par LLM

- [ ] Générer à partir de la transcription le contenu métier complet du ticket,
  notamment son titre et sa description structurée, avec un LLM maîtrisé et
  désactivable.
- [ ] Valider la sortie du modèle avec un contrat structuré avant tout envoi à
  GLPI. Les champs techniques et les valeurs imposées par la configuration
  restent déterministes.
- [ ] Extraire uniquement un numéro de rappel explicitement demandé dans la
  transcription et le distinguer du numéro d'origine fourni par Exchange.
- [ ] Conserver un contenu de repli déterministe lorsque le générateur est
  absent, indisponible ou produit une réponse invalide.
- [ ] Valider les règles de confidentialité avant d'envoyer une transcription
  à un modèle supplémentaire.

## Licence

Copyright © 2026 CTRE HOSPITALIER SPECIALISE VAUCLAIRE.

Ce projet est distribué sous la Licence publique de l'Union européenne,
version 1.2 (`EUPL-1.2`). Les versions ultérieures de l'EUPL restent
autorisées conformément aux conditions de la licence.

Le texte intégral est disponible dans [LICENSE](LICENSE) et l'avis de droits
d'auteur dans [NOTICE](NOTICE).
