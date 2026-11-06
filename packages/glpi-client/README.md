# Client GLPI partagé

Ce package contient l'adaptateur HTTP du contrat GLPI API V2.3.0 utilisé par
les applications du workspace.

Il expose deux ports distincts :

- `GlpiTicketCreationProtocol` pour garantir la création idempotente d'un
  ticket à partir de son `external_id` ;
- `GlpiTicketReaderProtocol` pour relire directement un ticket à partir de son
  identifiant GLPI persisté.

La lecture retourne uniquement l'identifiant, l'`external_id` et un
`TicketStatus` épinglé sur GLPI 11.0.7. Le client analyse `status.id` et ignore
`status.name`, dont le contenu dépend de la langue de l'instance. Seul
`TicketStatus.CLOSED` représente un ticket fermé ; `SOLVED` et `APPROVAL` sont
des états distincts.

Le package ne lit aucune variable d'environnement. Les applications composent
le client, fournissent les secrets OAuth et construisent les commandes métier.
Les valeurs fixes du ticket sont validées une fois dans `GlpiTicketDefaults`,
puis transportées par `GlpiTicketCreateCommand` ; elles ne font pas partie de
l'état du client HTTP.
La commande exige également une date d'ouverture UTC. Le client la convertit
avec `Europe/Paris` et l'envoie dans le champ métier GLPI `date` au format
RFC 3339. Il ne renseigne jamais `date_creation`, réservé à l'horodatage
technique de l'insertion par GLPI. La dépendance `tzdata` rend les règles de
changement d'heure disponibles dans les images Python minimales.
Les exceptions ne conservent ni corps HTTP brut ni secret.

En niveau `DEBUG`, le client émet pour une erreur de transport un diagnostic
expurgé : opération, hôte, types d'exception, `errno` et métadonnées de
validation TLS disponibles. Le message brut de l'exception, les headers, les
corps HTTP, les identifiants et les transcriptions ne sont jamais journalisés.
Le niveau est configuré par l'application appelante.

## Validation

Depuis la racine du workspace :

```bash
uv run --package mevo2glpi-glpi pytest packages/glpi-client/tests/unit
uv run --package mevo2glpi-glpi pytest -m glpi \
  packages/glpi-client/tests/integration/glpi -vv --maxfail=1
```
