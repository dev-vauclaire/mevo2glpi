# Contribuer à mevo2glpi

Merci de l'intérêt porté à mevo2glpi. Les contributions interétablissements
sont bienvenues lorsqu'elles préservent la sécurité, la confidentialité et la
portabilité du projet.

## Avant de proposer une modification

- ouvrir une issue GitHub non sensible pour décrire le besoin et son contexte ;
- ne jamais publier de donnée de patient, secret, certificat, identifiant réel
  ou information interne à un établissement ;
- vérifier la [compatibilité qualifiée](docs/adoption.md) et signaler toute
  hypothèse propre à l'établissement ;
- pour une vulnérabilité, suivre exclusivement [`SECURITY.md`](SECURITY.md).

## Développement

1. créer une branche dédiée depuis `main` ;
2. limiter chaque changement à un objectif clairement identifié ;
3. utiliser des fixtures synthétiques et déterministes ;
4. ajouter ou adapter les tests pertinents ;
5. exécuter les contrôles décrits dans la
   [stratégie de tests](docs/reference/tests.md) ;
6. proposer une Pull Request avec les impacts de sécurité, configuration,
   migration et exploitation.

Les commits suivent Conventional Commits, par exemple :

```text
feat(publisher): accept a new synthetic audio format
```

## Revue

Une contribution doit être relue avant fusion. La CI doit être verte et les
changements de contrat, de schéma ou de déploiement doivent inclure leur
documentation et leur stratégie de retour arrière.

Les contributions restent soumises à l'EUPL 1.2 du dépôt. Ne pas ajouter de
code ou de contenu dont la licence est incompatible ou dont les droits ne sont
pas établis.
