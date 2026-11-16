# Déployer mevo2glpi

Ce dossier contient les procédures ordonnées de déploiement de mevo2glpi.
Chaque environnement possède un index et un fichier par étape afin qu'une
opération puisse être exécutée, validée puis reprise sans parcourir un document
monolithique.

## Choisir le parcours

| Environnement | Objectif | Point d'entrée |
| --- | --- | --- |
| Développement | Exécuter tout ou partie du système sur une machine locale avec des services synthétiques | [Développement local](development/README.md) |
| Production | Déployer avec Dokploy et raccorder les services externes de l'établissement | [Production](production/README.md) |

Le développement permet d'activer séparément GLPI, Keycloak et la
transcription selon le scénario étudié. Le parcours de production considère
Microsoft 365, Keycloak et GLPI comme des services externes à la stack
mevo2glpi. La transcription peut être locale ou fournie par une API externe
compatible avec le contrat OpenAI.

## Convention des étapes

Les fichiers sont préfixés par leur ordre d'exécution. Une étape décrit, selon
le besoin :

1. son objectif et ses prérequis ;
2. les entrées et configurations nécessaires ;
3. les manipulations à effectuer ;
4. les commandes de validation ;
5. le résultat attendu ;
6. le diagnostic ou le retour arrière applicable ;
7. l'étape suivante.

Une étape facultative indique explicitement dans quels scénarios elle est
requise et les conséquences de son omission.

## Sécurité documentaire

- Ne jamais copier de secret, certificat privé, jeton ou donnée réelle dans le
  dépôt, une commande d'exemple ou un journal partagé.
- Employer uniquement des identifiants et contenus synthétiques dans les
  validations.
- Conserver les secrets dans les mécanismes prévus par l'environnement :
  fichiers protégés sur l'hôte, variables Dokploy ou gestionnaire de secrets de
  l'établissement.
- Vérifier les permissions et la chaîne de confiance TLS sans afficher les
  valeurs sensibles.

Les choix d'architecture sont détaillés dans la
[référence technique](../reference/architecture.md). Les recettes Docker et
leurs invariants sont indexés dans la
[documentation de l'infrastructure](../../infra/docker/README.md).
