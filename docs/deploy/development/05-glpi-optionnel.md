# Étape 5 — Choisir ou démarrer GLPI

## Objectif

Fournir une API GLPI V2.3.0 à automation et retention. Cette étape peut être
ignorée pour un profil limité au dashboard ou au publisher.

Deux options sont possibles :

1. GLPI 11.0.7 local et entièrement synthétique ;
2. GLPI de test externe, jamais l'instance de production.

## Option A — GLPI local

Démarrer GLPI et sa base MySQL privée :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml up --detach --build --wait glpi-test
```

Initialiser ensuite l'API V2, le client OAuth Password, l'utilisateur fictif et
la source `MEVO`. Le bootstrap est idempotent :

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml run --rm glpi-bootstrap
```

GLPI est accessible uniquement sur `http://127.0.0.1:18083`. MySQL n'est pas
publié sur l'hôte.

La configuration `.env.example` fournit les valeurs synthétiques cohérentes :

```dotenv
GLPI_BASE_URL=http://127.0.0.1:18083
GLPI_USERNAME=glpi-test-development-user
GLPI_CLIENT_ID=glpi-test-development-client
```

Les mots de passe et secrets restent ceux de `.env` et ne doivent jamais être
copiés dans un document ou un log.

### Récupérer la source MEVO

```bash
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml exec -T glpi-test php /opt/glpi-test/bootstrap_request_source.php
```

Reporter l'identifiant JSON `id` dans :

```dotenv
GLPI_TICKET_REQUEST_SOURCE_ID=IDENTIFIANT_LOCAL
```

## Option B — GLPI de test externe

Configurer l'URL, le compte technique, le client OAuth et l'identifiant de la
source propres à cette instance dans `.env`. Si une CA interne est nécessaire,
l'ajouter au store de confiance de la machine plutôt que désactiver TLS.

Ne jamais utiliser les identifiants du GLPI de production pour un développement
local. Les tickets de validation doivent rester entièrement synthétiques.

## Validation

Pour GLPI local :

```bash
curl --fail http://127.0.0.1:18083/api.php/v2.3.0/doc.json
docker compose --env-file .env --file infra/docker/development/docker-compose.test-services.yml ps glpi-db glpi-test
```

Pour un GLPI externe, contrôler d'abord DNS et TLS, puis l'obtention du token
avec les tests du client plutôt qu'en affichant les identifiants dans la ligne
de commande.

## Résultat attendu

`GLPI_BASE_URL` désigne une API V2.3.0 joignable, le compte de service peut
créer et lire des tickets, et `GLPI_TICKET_REQUEST_SOURCE_ID` appartient à
l'instance sélectionnée.

## Étape suivante

Configurer [l'authentification du dashboard](06-authentification.md).
