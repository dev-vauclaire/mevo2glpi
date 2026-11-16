# Déployer l'environnement de développement

Ce parcours exécute les applications Python depuis le workspace uv. Les
dépendances locales sont démarrées avec Docker et restent accessibles
uniquement sur `127.0.0.1`.

Les comptes, messages, audios et secrets utilisés doivent être exclusivement
synthétiques. Aucun certificat interne, compte réel, audio métier ou donnée
sensible ne doit être copié dans cet environnement.

## Choisir un profil

| Profil | Graph | GLPI | Authentification | Transcription | Processus applicatifs |
| --- | --- | --- | --- | --- | --- |
| Dashboard minimal | non requis | non requis | locale | non requise | migrations et web |
| OIDC local | non requis | non requis | Keycloak local | non requise | migrations et web |
| Pipeline simulé | simulateur | GLPI local | locale ou Keycloak | simulateur OpenAI-compatible | quatre applications persistantes |
| Pipeline avec modèle | simulateur | GLPI local ou externe de test | locale ou Keycloak | Parakeet local | quatre applications persistantes |

GLPI et la transcription sont optionnels uniquement lorsqu'aucun worker qui en
dépend n'est lancé. Un traitement complet exige un GLPI joignable et un
provider de transcription compatible OpenAI. Le publisher exige le simulateur
Microsoft Graph ; le développement local ne contacte jamais Microsoft 365.

## Étapes

1. [Vérifier les prérequis](01-prerequis.md).
2. [Préparer la configuration locale](02-configuration-locale.md).
3. [Démarrer PostgreSQL et migrer le schéma](03-postgresql.md).
4. [Démarrer Microsoft Graph simulé](04-microsoft-graph-simulator.md), si le
   publisher doit être exécuté.
5. [Choisir ou démarrer GLPI](05-glpi-optionnel.md), si automation ou retention
   doivent être exécutés.
6. [Configurer l'authentification](06-authentification.md).
7. [Choisir la transcription](07-transcription-optionnelle.md), si automation
   doit être exécuté.
8. [Lancer les applications](08-applications.md).
9. [Valider le scénario retenu](09-validation.md).
10. [Arrêter ou réinitialiser l'environnement](10-arret-et-reinitialisation.md).

## Ressources utilisées

| Ressource | Emplacement |
| --- | --- |
| PostgreSQL applicatif | `infra/docker/development/docker-compose.postgres.yml` |
| Simulateurs et GLPI | `infra/docker/development/docker-compose.test-services.yml` |
| Laboratoire Keycloak | `infra/docker/development/docker-compose.keycloak.yml` |
| Transcription Parakeet | `infra/docker/transcription/docker-compose.transcription.yml` |
| Configuration applicative | `.env` copié depuis `.env.example` |

Les fichiers Compose et les variables de référence restent la source de vérité.
Cette procédure explique leur assemblage sans recopier leurs contrats complets.

## Résultat attendu

Chaque service démarré correspond au profil choisi, ses dépendances sont
joignables et le dashboard identifie clairement l'environnement comme
`development`. Les services non nécessaires restent arrêtés.
