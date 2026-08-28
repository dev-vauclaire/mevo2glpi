# Client GLPI partagé

Ce package contient l'adaptateur HTTP du contrat GLPI API V2.3.0 utilisé par
les applications du workspace.

Il expose deux ports distincts :

- `GlpiTicketCreationProtocol` pour garantir la création idempotente d'un
  ticket à partir de son `external_id` ;
- `GlpiTicketReaderProtocol` pour relire directement un ticket à partir de son
  identifiant GLPI persisté.

La lecture retourne uniquement l'identifiant, l'`external_id` et un
`TicketStatus` épinglé sur GLPI 11.0.8. Le client analyse `status.id` et ignore
`status.name`, dont le contenu dépend de la langue de l'instance. Seul
`TicketStatus.CLOSED` représente un ticket fermé ; `SOLVED` et `APPROVAL` sont
des états distincts.

Le package ne lit aucune variable d'environnement. Les applications composent
le client, fournissent les secrets OAuth et construisent les commandes métier.
Les valeurs fixes du ticket sont validées une fois dans `GlpiTicketDefaults`,
puis transportées par `GlpiTicketCreateCommand` ; elles ne font pas partie de
l'état du client HTTP.
Les exceptions ne conservent ni corps HTTP brut ni secret.

## Validation

Depuis la racine du workspace :

```bash
uv run --package glpi-ticketing-glpi pytest packages/glpi-client/tests/unit
uv run --package glpi-ticketing-glpi pytest -m glpi \
  packages/glpi-client/tests/integration/glpi -vv --maxfail=1
```
