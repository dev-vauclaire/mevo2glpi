# 5. Préparer GLPI externe

La version qualifiée est **GLPI 11.0.7** et l'application épingle explicitement
l'API **V2.3.0**.

## Préparer l'instance

Disposer :

- d'une URL directe qui ne redirige pas vers un portail SSO interactif ;
- de l'API V2 activée ;
- d'un utilisateur technique réservé à mevo2glpi ;
- d'un client OAuth Password avec le scope `api` ;
- des droits minimaux pour créer et consulter les tickets attendus ;
- d'une source de demande `MEVO` et de son identifiant local ;
- du fuseau `Europe/Paris` pour le compte technique ou de son héritage global.

L'identifiant de la source est propre à l'instance. Ne réutiliser ni celui d'un
autre GLPI ni celui d'un environnement de test.

## Vérifier l'accès direct et TLS

Depuis la VM :

```bash
curl --fail --silent --show-error \
  https://NOM_GLPI_PRODUCTION/api.php/v2.3.0/doc.json \
  >/dev/null
```

Une redirection vers un portail interactif n'est pas compatible avec le client
machine-to-machine. Une erreur TLS doit être corrigée dans la chaîne de
confiance, jamais contournée.

## Variables attendues

```dotenv
GLPI_BASE_URL=https://NOM_GLPI_PRODUCTION
GLPI_USERNAME=VALEUR_DOKPLOY
GLPI_PASSWORD=VALEUR_DOKPLOY
GLPI_CLIENT_ID=VALEUR_DOKPLOY
GLPI_CLIENT_SECRET=VALEUR_DOKPLOY
GLPI_HTTP_TIMEOUT_SECONDS=10
GLPI_TICKET_TYPE=1
GLPI_TICKET_REQUEST_SOURCE_ID=IDENTIFIANT_LOCAL_DE_MEVO
GLPI_TICKET_URGENCY=3
GLPI_TICKET_IMPACT=3
```

Ne pas placer les identifiants dans une commande, un fichier suivi ou un
journal partagé. Le test d'obtention du token sera effectué depuis le worker
avec sa configuration finale.

## Porte de validation

- [ ] la documentation V2.3.0 répond sans redirection ;
- [ ] le certificat est validé depuis la VM ;
- [ ] le compte technique et le client OAuth sont réservés à l'application ;
- [ ] leurs droits sont limités au besoin ;
- [ ] la source `MEVO` et son identifiant local sont vérifiés ;
- [ ] le fuseau GLPI est `Europe/Paris`.

Références : [API REST V2 de GLPI](https://help.glpi-project.org/documentation/modules/configuration/general/api/restful-api-v2)
et [clients OAuth GLPI](https://help.glpi-project.org/documentation/modules/configuration/oauth-clients).

Étape suivante : [PostgreSQL et stockage](06-postgresql-et-stockage.md).
