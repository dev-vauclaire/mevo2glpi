# Microsoft Graph simulator

Simulateur stateful réservé au staging. Il expose le sous-ensemble Graph utilisé
par le publisher et une administration locale permettant d'injecter des messages
WAV ou OGG synthétiques.

Il ne simule pas Microsoft Entra ID et ne doit recevoir aucune donnée réelle.

```text
MICROSOFT_GRAPH_SIMULATOR_HOST=0.0.0.0
MICROSOFT_GRAPH_SIMULATOR_PORT=8080
MICROSOFT_GRAPH_SIMULATOR_MAILBOX_ID=<UUID fictif>
MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN=<secret staging>
```

Depuis le conteneur :

```bash
glpi-ticketing-microsoft-graph-simulator-admin seed --extension wav
glpi-ticketing-microsoft-graph-simulator-admin seed --extension ogg
glpi-ticketing-microsoft-graph-simulator-admin reset
```
