# Application Dokploy Microsoft Graph Simulator

Cette image simule uniquement le sous-ensemble de Microsoft Graph utilisé par
le publisher. Elle est strictement réservée au développement et au staging.

## Construction

Créer une application Dokploy de type **Dockerfile** avec :

| Réglage | Valeur |
| --- | --- |
| Contexte Docker | `.` (racine du dépôt) |
| Dockerfile | `infra/docker/providers/microsoft-graph-simulator/Dockerfile` |
| Port interne | `8080` |
| Domaine / port public | aucun |

Le contexte ne doit pas être limité à ce dossier : la construction utilise le
`pyproject.toml`, le `uv.lock` et les manifestes du workspace racine.

Copier les variables de [`.env.example`](.env.example) dans l'onglet
**Environment** de l'application, puis remplacer le jeton fictif. Ce fichier
n'est pas chargé automatiquement par Dokploy.

## Réseau et fixtures

Le réseau `gta-staging-simulators-network` doit être un réseau Docker Swarm
`overlay`, `attachable` et `internal`. Dans **Advanced > Swarm Settings >
Network**, rattacher l'application à ce réseau externe privé avec l'alias
`gta-staging-microsoft-graph`.

Ajouter le bind mount suivant, sans demander à Docker de créer le répertoire :

```text
/srv/gta-staging/audio-fixtures:/fixtures/audio:ro
```

Le répertoire hôte doit exister, être lisible par l'UID/GID `10001` et ne
contenir que des audios fictifs non sensibles.

## Exécution et limite connue

Le Dockerfile impose un processus non privilégié avec l'UID/GID `10001` et
intègre un healthcheck sur `/health` au port interne `8080`. Configurer dans
Dokploy une politique de redémarrage automatique et des limites de ressources
adaptées au staging.

L'interface Application documentée par Dokploy ne permet pas de reproduire de
façon déclarative le système de fichiers racine en lecture seule, `cap_drop:
ALL`, `no-new-privileges`, la limite de processus, le `tmpfs` durci et la
rotation JSON du Compose. Ces protections ne peuvent pas être imposées par le
Dockerfile seul. Le Compose de transition reste donc la variante durcie jusqu'à
ce que cette limitation soit acceptée ou compensée côté plateforme.
