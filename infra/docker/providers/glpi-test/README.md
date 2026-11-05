# GLPI de staging autonome

Cette recette déploie une instance **GLPI 11.0.7 exclusivement réservée au
staging** lorsque le GLPI de test externe n'est pas disponible. Elle ne doit
jamais être utilisée en production ni recevoir de donnée réelle.

Le Compose contient :

- `glpi-db`, MySQL privé et persistant ;
- `glpi-test`, publié uniquement par Traefik/Dokploy sur son port interne `80` ;
- `glpi-bootstrap`, tâche one-shot idempotente qui active l'API V2, crée le
  compte et le client OAuth de test, puis la source de demande `MEVO`.

## Déploiement Dokploy

Créer une application **Docker Compose** depuis le dépôt et renseigner :

```text
Compose Path: ./infra/docker/providers/glpi-test/docker-compose.staging.yml
Environment:  contenu adapté de staging.env.example
```

Associer ensuite un domaine HTTPS au service `glpi-test`, port interne `80`.
MySQL et `glpi-bootstrap` ne doivent recevoir ni domaine ni port publié. Les
volumes et le réseau MySQL sont nommés explicitement afin de survivre aux
redéploiements.

Après le premier déploiement, vérifier que `glpi-bootstrap` se termine avec le
code `0`. Il peut être relancé sans créer de doublon. Récupérer ensuite
l'identifiant local de la source `MEVO` depuis le terminal `glpi-test` :

```bash
php /opt/glpi-test/bootstrap_request_source.php
```

Reporter la valeur JSON `id` dans `GLPI_TICKET_REQUEST_SOURCE_ID` et utiliser
le domaine HTTPS comme `GLPI_BASE_URL` dans l'application mevo2glpi.

## Invariants de sécurité

- toutes les valeurs `CHANGE_ME` sont remplacées avant le déploiement ;
- l'utilisateur et le client OAuth commencent par `glpi-test-` ;
- les identifiants ne sont pas partagés avec la production ;
- le domaine GLPI utilise un certificat approuvé par les workers ;
- aucun port MySQL n'est publié ;
- les volumes ne sont supprimés qu'après validation explicite de la perte des
  données de staging.

Changer les identifiants OAuth après le premier bootstrap provoque
volontairement un échec. Une rotation doit être préparée explicitement ; il ne
faut pas supprimer les volumes pour contourner cette protection sans avoir
validé la perte des données.
