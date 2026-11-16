# 4. Configurer l'Identity Provider OpenID Connect

La production utilise un Keycloak externe. mevo2glpi communique avec lui par le
protocole standard OpenID Connect et ne dépend d'aucune API propriétaire
Keycloak. Aucun Keycloak n'est déployé dans la stack applicative.

## Créer le client

Dans Keycloak :

1. créer un client confidentiel dédié au web mevo2glpi ;
2. activer le flux Authorization Code ;
3. désactiver les flux non utilisés ;
4. définir exactement l'URI de redirection publique :
   `https://NOM_DASHBOARD/auth/oidc/callback` ;
5. créer le rôle d'accès `mevo2glpi-access` ;
6. exposer les rôles autorisés dans une claim multivaluée `roles` ;
7. affecter ce rôle uniquement aux groupes ou comptes autorisés.

Le client secret reste dans Dokploy. Aucun token OIDC, cookie de session ou
identifiant personnel ne doit être copié dans un ticket ou un journal partagé.

## Vérifier le document de découverte

Depuis la VM :

```bash
curl --fail --silent --show-error \
  https://NOM_IDP/REALM/.well-known/openid-configuration \
  >/dev/null
```

Le certificat doit être validé par le bundle installé à l'étape précédente.
L'URL d'émetteur configurée doit correspondre exactement à la claim `iss`
émise par Keycloak, sans réécriture implicite HTTP/HTTPS.

## Variables attendues

```dotenv
WEB_AUTH_MODE=oidc
WEB_AUTH_SESSION_SECRET=VALEUR_ALEATOIRE_DOKPLOY_AU_MOINS_32_CARACTERES
WEB_AUTH_SESSION_MAX_AGE_SECONDS=1800
WEB_AUTH_COOKIE_SECURE=true
WEB_AUTH_REQUIRED_ROLE=mevo2glpi-access
WEB_AUTH_ROLES_CLAIM=roles
WEB_AUTH_ISSUER_URL=https://NOM_IDP/REALM
WEB_AUTH_CLIENT_ID=mevo2glpi-web
WEB_AUTH_CLIENT_SECRET=VALEUR_DOKPLOY
WEB_AUTH_REDIRECT_URI=https://NOM_DASHBOARD/auth/oidc/callback
PUBLIC_BASE_URL=https://NOM_DASHBOARD
```

En production, ne pas utiliser `WEB_AUTH_MODE=local` comme repli automatique
si Keycloak est indisponible. Une indisponibilité OIDC doit fermer
l'accès, pas contourner l'authentification.

## Porte de validation

- [ ] le document de découverte répond avec une chaîne TLS valide ;
- [ ] l'URI de redirection est exacte et uniquement en HTTPS ;
- [ ] un compte autorisé reçoit la claim et le rôle attendus ;
- [ ] un compte sans rôle est refusé ;
- [ ] les cookies applicatifs sont `Secure`, `HttpOnly` et `SameSite=Lax` ;
- [ ] aucun secret OIDC n'est enregistré dans Git.

Étape suivante : [GLPI externe](05-glpi-externe.md).
