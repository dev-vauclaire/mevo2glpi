# Référence des tests

Ce document décrit la stratégie de validation transversale du dépôt et les
commandes à exécuter depuis sa racine. Les README placés avec chaque application
ou package restent la référence pour leurs scénarios ciblés.

Les tests utilisent exclusivement des identifiants, messages et audios
synthétiques. Aucun secret, certificat privé, jeton, fichier d'environnement ou
contenu réel de production ne doit être utilisé.

## Niveaux de validation

| Niveau | Marqueur Pytest | Périmètre | Dépendance principale |
| --- | --- | --- | --- |
| Unitaire et contractuel déterministe | aucun marqueur spécialisé | Règles métier, parseurs, clients simulés, API et boucles de workers | Aucune infrastructure externe |
| Persistance | `database` | Migrations, contraintes, repositories, transactions et verrous | PostgreSQL Testcontainers |
| Déploiement | `deployment` | Dockerfiles, rendu Compose, réseaux, volumes, sécurité et healthchecks | Docker et Docker Compose |
| Contrat GLPI | `glpi` | OAuth, API GLPI V2.3.0 et création de ticket | GLPI 11.0.7 et MySQL jetables |
| Modèle local réel | `transcription_model` | Chargement et inférence du GGUF Parakeet Ultra | Modèle fourni localement et CPU |
| Parcours aval complet | `e2e` | Migration, automation, GLPI, web et rétention | PostgreSQL, GLPI et provider de transcription jetables |
| Parcours nominal Parakeet | `e2e` et `transcription_model` | Audio synthétique, transcription CPU réelle et ticket GLPI corrélé | Image Parakeet épinglée, modèle GGUF, PostgreSQL et GLPI jetables |

Les marqueurs sont déclarés dans le [`pyproject.toml`](../../pyproject.toml).

## Matrice de preuves

Cette matrice relie les principaux risques du produit à leur preuve actuelle.
Une porte **systématique** s'applique à tout changement. Une porte
**conditionnelle** devient obligatoire lorsque le périmètre indiqué est
modifié.

| Risque couvert | Preuve actuelle | Exécution | Porte de validation |
| --- | --- | --- | --- |
| Régression des règles métier, clients et workers | Tests unitaires et contractuels déterministes | CI applications ou providers selon les chemins modifiés | Systématique |
| Schéma, migrations, repositories et transactions | Suite `database` sur PostgreSQL jetable | CI applications | Conditionnelle à toute modification de persistance |
| Contrats des simulateurs Graph et OpenAI-compatible | Tests providers et contrat conteneurisé des simulateurs | CI providers | Conditionnelle aux providers et à leur infrastructure |
| Dockerfiles, Compose, réseaux, volumes et durcissement runtime | Suite `deployment` | Partielle en CI, complète en local | Conditionnelle à toute modification de `infra/docker` ou d'une image |
| OAuth, API GLPI, création et suivi d'un ticket | Suite `glpi` avec GLPI et MySQL jetables | Manuelle | Conditionnelle au client GLPI, à l'automatisation ou à la rétention |
| Résilience du pipeline aval OpenAI-compatible | E2E avec simulateur de transcription | Manuelle | Conditionnelle à l'automatisation, au web ou à la rétention |
| Chargement et inférence du modèle Parakeet réel | Suite `transcription_model` | Manuelle | Conditionnelle à l'API, au modèle ou à l'image de transcription |
| Parcours nominal Parakeet vers GLPI | E2E réel CPU | Manuelle | Conditionnelle à une release de transcription |
| Authentification locale et OIDC | Tests web, persistance et contrats du laboratoire Keycloak | Partielle en CI | Recette staging obligatoire pour un fournisseur OIDC réel |
| Entra ID, Exchange Online, Traefik, TLS et Dokploy | Recette opérateur sur données fictives | Staging manuel | Obligatoire avant toute promotion en production |

La colonne « Exécution » décrit l'état du dépôt, pas une intention future. Une
suite manuelle n'est donc pas implicitement couverte par une CI verte.

## Préparation

Prérequis communs :

- Python 3.12 et `uv` ;
- Docker Engine et Docker Compose pour les suites conteneurisées ;
- des ports loopback éphémères disponibles ;
- suffisamment de mémoire et d'espace disque pour les images et modèles ;
- aucune autre suite Docker lourde de ce dépôt exécutée en parallèle.

Synchroniser l'environnement verrouillé :

```bash
uv sync --locked --all-packages --all-groups
```

## Qualité et suite déterministe

```bash
uv run ruff format --check .
uv run ruff check .
uv run --all-packages pytest apps packages tests \
  -m "not database and not deployment and not glpi and not transcription_model and not e2e" \
  --maxfail=1
uv run pytest providers --maxfail=1
```

Les workflows `ci-apps.yml` et `ci-providers.yml` exécutent ces contrôles selon
les chemins modifiés. La CI des applications valide également PostgreSQL, les
migrations et les principaux contrats statiques des fichiers Compose.

### Périmètre exact de la CI

La CI **applications** exécute :

- Ruff sur l'ensemble du dépôt ;
- la suite déterministe de `apps/`, `packages/` et `tests/` ;
- toute la suite `database` ;
- le rendu des Compose applicatifs de production et de staging ;
- des contrats ciblés sur les Dockerfiles, PostgreSQL de développement,
  l'image GLPI canonique et la stack de transcription.

Elle n'exécute pas toute la suite `deployment` et ne construit donc pas les
cinq images applicatives à chaque changement.

La CI **providers** exécute :

- Ruff et tous les tests sous `providers/` ;
- le contrat conteneurisé des simulateurs Microsoft Graph et
  OpenAI-compatible.

Les suites `glpi`, `transcription_model` et `e2e` ne sont exécutées par aucun
workflow. Elles restent des portes locales ou de staging selon la matrice
ci-dessus.

## Suites spécialisées

### PostgreSQL et migrations

```bash
uv run --all-packages pytest -m database -vv --maxfail=1
```

La suite couvre une base neuve et les chemins de mise à niveau explicitement
pris en charge. Les fixtures utilisent une base isolée et détruisent les
ressources qu'elles ont créées.

### Contrats de déploiement

```bash
uv run pytest -m deployment tests/deployment providers/tests/deployment \
  -vv --maxfail=1
```

Ces tests valident les manifests et les contrats Docker. Ils ne constituent pas
un déploiement Dokploy et ne contactent aucun service réel.

### GLPI

```bash
uv run --all-packages pytest -m glpi -vv --maxfail=1
```

La stack GLPI est jetable. Cette commande couvre le client partagé et
l'intégration du worker de rétention. Elle ne doit jamais être remplacée dans
cette suite par une instance de staging ou de production.

### Modèle Parakeet Ultra réel

Les tests ordinaires de `transcription-api` injectent un moteur et ne chargent
pas le modèle. Le smoke test réel exige un GGUF fourni explicitement :

```bash
TRANSCRIPTION_MODEL_TEST_PATH=/chemin/vers/parakeet-ultra-Q8_0.gguf \
uv run pytest -m transcription_model \
  apps/transcription-api/tests/integration/test_real_model.py -vv --maxfail=1
```

Ce test vérifie le chargement et l'inférence, pas la qualité fonctionnelle. La
qualité du français doit être qualifiée en staging avec des audios fictifs
représentatifs.

### Parcours E2E aval

Le parcours nominal construit l'API CPU Parakeet épinglée, injecte un audio
français synthétique, attend la fin du job puis vérifie les événements de
transcription et le ticket GLPI corrélé :

```bash
uv run pytest -m "e2e and transcription_model" \
  tests/e2e/test_parakeet_nominal.py -vv --maxfail=1
```

Le premier build télécharge les couches et le modèle GGUF épinglé, puis
construit l'image locale. Il exige donc un accès aux sources configurées, du
temps, de l'espace disque et suffisamment de mémoire. L'inférence reste
strictement exécutée sur CPU.

La régression du pipeline avec le simulateur OpenAI-compatible est
séquentielle :

```bash
uv run --all-packages pytest -m "e2e and not transcription_model" \
  tests/e2e \
  -vv --maxfail=1
```

L'E2E démarre la migration, le web, le worker d'automatisation et le worker de
rétention. Il reproduit la frontière durable du publisher avec un helper
réservé aux tests, mais ne contacte pas Microsoft 365 et ne démarre pas le
publisher réel. Les contrats Microsoft Graph et l'orchestration du publisher
sont couverts séparément par ses tests et le simulateur stateful.

Le scénario nominal qualifie le contrat OpenAI-compatible et le modèle
Parakeet Ultra sur un audio synthétique ; les autres scénarios de résilience
utilisent le simulateur OpenAI-compatible jetable. Aucun de ces parcours ne
qualifie Traefik, les certificats TLS ou un tenant Entra ID réel. La topologie
et les scénarios sont détaillés dans le
[README de la suite E2E](../../tests/e2e/README.md).

## Séquentialité et nettoyage

Les suites `database`, `deployment`, `glpi` et `e2e` ne doivent
pas être parallélisées entre elles. N'utilisez ni `pytest-xdist`, ni l'option
`-n` pour les suites qui partagent le daemon Docker.

Chaque fixture ne supprime que les conteneurs, réseaux et volumes qu'elle a
créés. Une erreur de disponibilité doit rester bornée, déclencher ce nettoyage
et produire un diagnostic expurgé. Ne lancez jamais de nettoyage Docker global
pour terminer une suite de ce dépôt.

## Frontières avec la recette opérateur

Les tests automatisés prouvent les contrats logiciels dans un environnement
jetable. La procédure de staging complète cette preuve avec :

- Microsoft Graph ou son simulateur selon le mode retenu ;
- le provider de transcription réellement sélectionné ;
- une instance GLPI de test ;
- le routage Dokploy et les certificats internes ;
- une validation fonctionnelle sur des audios fictifs représentatifs.

Une panne ne doit jamais être injectée dans Exchange, GLPI ou un provider de
production pour valider un scénario de test.

## Portes avant fusion et release

### Pour tout changement

1. exécuter les tests ciblés du composant modifié ;
2. exécuter Ruff et la suite déterministe pertinente ;
3. vérifier que le workflow GitHub attendu est déclenché par ses filtres de
   chemins et qu'il termine avec succès ;
4. contrôler que les journaux et artefacts ne contiennent ni secret ni donnée
   réelle.

### Selon le périmètre

- persistance ou migration : exécuter toute la suite `database` ;
- `infra/docker`, Dockerfile ou topologie réseau : exécuter toute la suite
  `deployment`, pas seulement `docker compose config` ;
- client GLPI, création de ticket ou rétention GLPI : exécuter `glpi` ;
- automation, web ou rétention aval : exécuter l'E2E avec simulateur ;
- API, image ou modèle de transcription : exécuter le smoke test du modèle et
  l'E2E Parakeet nominal ;
- authentification OIDC, Entra ID, Exchange, TLS ou routage : compléter les
  tests automatisés par la recette de staging correspondante.

### Avant une release

La CI doit être verte sur le SHA candidat. Les portes conditionnelles du
changement doivent avoir réussi, puis le même SHA doit être validé en staging
avec la [procédure CI/CD](ci-cd.md). Un tag ne doit pas être créé à partir d'un
autre commit.

## Limites connues

L'état des lieux fait apparaître les limites suivantes :

- la suite `deployment` complète n'est pas encore exécutée par la CI
  applications ;
- aucun E2E automatisé ne contacte un tenant Entra ID ou Exchange Online réel
  et ne démarre le publisher dans cette frontière réelle ;
- aucun E2E ne qualifie un fournisseur OIDC réel, Traefik, les certificats ou
  Dokploy ;
- la CI ne fixe pas encore de seuil de couverture et n'exécute pas de contrôle
  dédié de typage, de vulnérabilités, d'images ou de secrets ;
- la qualité linguistique de la transcription reste une validation
  fonctionnelle de staging, distincte de la réussite technique du modèle.

Ces limites ne doivent pas être contournées par des appels aux services de
production. Leur automatisation sera priorisée selon le risque et le coût
d'exploitation. Toute réorganisation de `infra/docker` devra également mettre
à jour les filtres de chemins des deux workflows et conserver les contrats de
rendu des recettes prises en charge.
