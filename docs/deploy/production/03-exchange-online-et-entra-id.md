# 3. Configurer Exchange Online et Entra ID

Le publisher utilise le flux applicatif `client_credentials` avec un certificat
X.509. Exchange Online Application RBAC limite cette identité à la seule boîte
vocale dédiée. Ne lui attribuer aucun droit Graph tenant-wide qui contournerait
ce périmètre.

## Préparer l'application Entra ID

1. Créer une App Registration **single-tenant** dédiée à mevo2glpi.
2. Charger uniquement le certificat public X.509 associé à la clé privée qui
   sera installée sur la VM.
3. Relever sans les publier :
   - le Tenant ID ;
   - le Client ID de l'App Registration ;
   - l'Object ID de l'Enterprise Application ;
   - l'Object ID Entra de l'utilisateur représentant la boîte dédiée.
4. Ne pas créer de client secret si l'authentification par certificat est
   retenue.

`PUBLISHER_MAILBOX_ID` attend l'Object ID Entra de la boîte, pas son Exchange
GUID ni son adresse électronique.

## Restreindre l'accès à la boîte

L'exemple ci-dessous utilise l'attribut personnalisé 15. Remplacer les valeurs
symboliques par les valeurs validées localement, sans les consigner dans Git.

```powershell
Connect-ExchangeOnline

Get-Mailbox -Identity BOITE_DEDIEE | Format-List DisplayName,ExternalDirectoryObjectId,CustomAttribute15

Set-Mailbox -Identity BOITE_DEDIEE -CustomAttribute15 "MEVO2GLPI-PUBLISHER"

New-ManagementScope `
  -Name "mevo2glpi-publisher-mailbox" `
  -RecipientRestrictionFilter "CustomAttribute15 -eq 'MEVO2GLPI-PUBLISHER'"

New-ServicePrincipal `
  -AppId APP_REGISTRATION_CLIENT_ID `
  -ObjectId ENTERPRISE_APPLICATION_OBJECT_ID `
  -DisplayName "mevo2glpi publisher"

New-ManagementRoleAssignment `
  -Name "mevo2glpi-publisher-mail-readwrite" `
  -Role "Application Mail.ReadWrite" `
  -App SERVICE_PRINCIPAL_OBJECT_ID `
  -CustomResourceScope "mevo2glpi-publisher-mailbox"
```

Tester une boîte comprise dans le scope et une boîte témoin exclue :

```powershell
Test-ServicePrincipalAuthorization `
  -Identity SERVICE_PRINCIPAL_OBJECT_ID `
  -Resource BOITE_DEDIEE

Test-ServicePrincipalAuthorization `
  -Identity SERVICE_PRINCIPAL_OBJECT_ID `
  -Resource BOITE_HORS_SCOPE
```

Le premier test doit être autorisé et le second refusé. La propagation peut
prendre de 30 minutes à 2 heures.

## Invariants de la boîte

- La boîte est exclusivement réservée à ce flux.
- Au premier démarrage, l'Inbox ne contient que des messages que l'application
  est autorisée à traiter ; le publisher consomme tout le backlog.
- Les dossiers `Processed` et `Rejected` sont créés automatiquement et de
  manière idempotente ; ne pas les créer manuellement.
- Un message est déplacé après traitement selon le résultat d'ingestion.

## Variables attendues

```dotenv
PUBLISHER_ENTRA_TENANT_ID=VALEUR_DOKPLOY
PUBLISHER_ENTRA_CLIENT_ID=VALEUR_DOKPLOY
PUBLISHER_MAILBOX_ID=VALEUR_DOKPLOY
PUBLISHER_ENTRA_PRIVATE_KEY_PEM_HOST_PATH=/etc/mevo2glpi/secrets/publisher-private-key.pem
PUBLISHER_ENTRA_CERTIFICATE_PEM_HOST_PATH=/etc/mevo2glpi/secrets/publisher-certificate.pem
PUBLISHER_ENTRA_PRIVATE_KEY_PASSPHRASE=
```

Laisser la passphrase vide uniquement si la clé privée n'est pas chiffrée ;
sinon, la fournir comme secret Dokploy.

Les fichiers et leurs permissions sont préparés à l'étape
[PostgreSQL et stockage](06-postgresql-et-stockage.md).

## Porte de validation

- [ ] le certificat public chargé dans Entra correspond à la clé privée ;
- [ ] l'identité applicative possède uniquement le rôle Exchange requis ;
- [ ] la boîte dédiée est autorisée et la boîte témoin est refusée ;
- [ ] les quatre identifiants nécessaires sont consignés dans le gestionnaire
  de secrets, jamais dans Git ;
- [ ] le backlog initial a été examiné avant d'autoriser le publisher.

Références : [authentification applicative Graph](https://learn.microsoft.com/en-us/graph/auth-v2-service),
[Exchange Application RBAC](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac)
et [certificats Entra](https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-credentials).

Étape suivante : [Identity Provider OpenID Connect](04-keycloak-externe.md).
