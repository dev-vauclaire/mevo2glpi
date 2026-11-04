# Microsoft Graph simulator

Simulateur stateful réservé au développement, au staging et aux tests. Il expose
le sous-ensemble Graph utilisé par le publisher et une administration permettant d'injecter des
scénarios déterministes ou un fichier WAV/OGG choisi par l'opérateur.

Il ne simule pas Microsoft Entra ID. Les fichiers choisis doivent être des
enregistrements de test fictifs, sans donnée patient ni donnée métier réelle.

```text
MICROSOFT_GRAPH_SIMULATOR_HOST=0.0.0.0
MICROSOFT_GRAPH_SIMULATOR_PORT=8080
MICROSOFT_GRAPH_SIMULATOR_MAILBOX_ID=<UUID fictif>
MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN=<clé synthétique>
MICROSOFT_GRAPH_SIMULATOR_AUDIO_FIXTURES_HOST_PATH=/srv/gta-staging/audio-fixtures
```

Le Compose de staging monte le dernier répertoire en lecture seule dans
`/fixtures/audio`. Il doit exister sur la VM avant le déploiement et rester
lisible par l'UID/GID `10001` du conteneur. En développement, utiliser les
scénarios intégrés décrits dans la procédure locale.

Depuis le conteneur :

```bash
glpi-ticketing-microsoft-graph-simulator-admin list-scenarios
glpi-ticketing-microsoft-graph-simulator-admin seed-suite
glpi-ticketing-microsoft-graph-simulator-admin seed --scenario valid-wav
glpi-ticketing-microsoft-graph-simulator-admin seed --scenario valid-ogg
glpi-ticketing-microsoft-graph-simulator-admin seed --scenario valid-wav-mulaw
glpi-ticketing-microsoft-graph-simulator-admin seed --audio-path /fixtures/audio/test-vocal.wav
glpi-ticketing-microsoft-graph-simulator-admin reset
```

`seed-suite` injecte une fois chaque scénario dans l'ordre du catalogue et
affiche un manifeste JSON contenant les identifiants des messages créés. La
commande n'attend pas leur traitement et ne réinitialise pas la boîte. Une
nouvelle exécution crée donc une nouvelle série de messages et, pour les cas
acceptés, de nouveaux jobs et tickets.

| Scénario | Résultat publisher attendu |
|---|---|
| `valid-wav`, `valid-ogg`, `valid-wav-alaw`, `valid-wav-mulaw` | accepté, message `Processed` |
| `no-attachment` | rejet `audio_attachment_missing` |
| `two-audio-attachments` | rejet `attachment_count_invalid` |
| `empty-audio` | rejet `audio_empty` |
| `unsupported-format` | rejet `audio_format_not_supported` |
| `corrupt-wav` | rejet `wav_header_invalid` |
| `corrupt-ogg` | rejet `ogg_header_invalid` |
| `too-large` | rejet `audio_too_large` |
| `valid-audio-with-inline` | accepté ; l'image inline est ignorée |
| `non-file-attachment` | rejet `attachment_kind_invalid` |
| `silent-wav` | accepté par le publisher ; résultat dépendant du transcripteur |

`--scenario` et `--audio-path` sont mutuellement exclusifs. Le fichier indiqué
doit être visible depuis le conteneur, porter l'extension `.wav` ou `.ogg`, ne
pas être vide et ne pas dépasser 25 Mio. Son chemin et son nom d'origine ne
sont pas transmis à l'API Graph simulée. Son contenu reste uniquement en
mémoire jusqu'au redémarrage ou à la commande `reset`. Cette commande ne
supprime pas le fichier source monté depuis la VM ; l'opérateur doit le retirer
après la recette. Aucun audio patient, message de production ou autre donnée
sensible ne doit être placé dans ce répertoire.
