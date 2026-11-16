# 10. Valider l'installation

La validation combine l'état Dokploy, l'identité de version, les dépendances
externes et un traitement synthétique de bout en bout.

## Contrôles techniques

### Web et authentification

- [ ] le domaine répond en HTTPS avec la chaîne attendue ;
- [ ] HTTP redirige vers HTTPS s'il est ouvert ;
- [ ] `/health` répond avec le code `200` ;
- [ ] `/api/version` ou le dashboard affiche `production`, le tag et le SHA ;
- [ ] un utilisateur OIDC avec `mevo2glpi-access` accède au dashboard ;
- [ ] un utilisateur sans ce rôle est refusé ;
- [ ] logout révoque la session navigateur attendue.

### Services internes

- [ ] les trois heartbeats de worker sont récents ;
- [ ] les runtimes sont `running`, sans UUID courant inattendu ;
- [ ] PostgreSQL et le volume audio sont joignables par les healthchecks ;
- [ ] le provider de transcription répond ;
- [ ] aucun job ne reste anormalement `pending`, `processing` ou
  `retry_scheduled`.

Les healthchecks ne prouvent pas l'accès à Graph, GLPI ou au provider externe.
Contrôler aussi les logs expurgés :

- publisher : découverte Graph sans `401`, `403` ou `transport_error` ;
- automation : aucune erreur TLS, transcription ou GLPI ;
- retention : aucune erreur TLS ou GLPI.

## Recette synthétique de bout en bout

Utiliser un message et un audio entièrement fictifs, sans identité, numéro de
téléphone réel, donnée de patient ou information interne sensible.

1. envoyer un message de recette dans la boîte dédiée avec un audio supporté ;
2. vérifier son apparition dans le dashboard ;
3. vérifier son passage de `pending` à `processing`, puis `completed` ;
4. vérifier le déplacement Exchange dans `Processed` ;
5. vérifier la création d'un unique ticket GLPI avec :
   - le type et la source `MEVO` attendus ;
   - une date d'ouverture cohérente avec la réception du message ;
   - le texte synthétique attendu ;
   - un lien HTTPS vers l'audio ;
6. ouvrir le lien avec un utilisateur autorisé ;
7. consigner le résultat, le tag et le SHA sans copier le contenu du ticket.

Ne pas répéter l'envoi pour contourner une anomalie : cela créerait une
nouvelle ingestion et potentiellement un nouveau ticket. Diagnostiquer le job
existant.

## Résultat attendu

L'installation est validée uniquement si :

- l'identité déployée est exacte ;
- l'authentification et le contrôle de rôle fonctionnent ;
- les trois workers sont opérationnels ;
- le message synthétique produit un seul ticket GLPI ;
- aucun secret ou contenu sensible n'apparaît dans les logs.

Pour une installation existante, suivre ensuite la
[procédure de mise à jour](11-mise-a-jour.md).
