# Procédure de déploiement en staging

## Objectif et périmètre

Cette procédure décrit l'installation initiale et la recette de l'environnement
de staging dans Dokploy. Cet environnement valide le pipeline complet sans
utiliser de boîte Exchange, de ticket GLPI ou d'audio de production :

- Microsoft Graph est remplacé par un simulateur stateful ;
- la transcription CPU utilise Parakeet Ultra et `transcribe.cpp` dans une
  stack indépendante ;
- PostgreSQL est une instance externe dédiée au staging ;
- GLPI est une instance de test externe accessible en HTTPS ;
- les cinq applications sont déployées dans une troisième stack.

Une mise à jour d'un staging existant suit la
[procédure de mise à jour](mise-a-jour.md). Les contrats techniques des stacks
restent décrits dans les README des
[providers de test](../../infra/docker/providers/README.md) et de la
[transcription](../../infra/docker/transcription/README.md).

## Prérequis

Avant toute installation :

1. disposer d'une VM Docker avec Dokploy et d'un accès opérateur ;
2. sélectionner une référence Git et relever son SHA complet ;
3. créer une base et un rôle PostgreSQL exclusivement réservés au staging ;
4. disposer d'un GLPI de test, de ses identifiants OAuth et de l'identifiant
   local de la source `MEVO` ;
5. installer sur la VM un bundle de CA validant GLPI et PostgreSQL ;
6. réserver des valeurs distinctes de la production pour les réseaux, le
   volume audio et les clés des simulateurs ;
7. utiliser uniquement des messages et audios fictifs, sans donnée patient ni
   donnée issue de la production.

Créer une seule fois les réseaux partagés entre projets :

```bash
sudo docker network create --driver bridge --internal gta-staging-simulators-network
sudo docker network create --driver bridge --internal gta-staging-transcription-network
```

Le réseau de la base et `dokploy-network` doivent également exister avec les
noms déclarés dans l'environnement Dokploy.

Préparer le répertoire facultatif d'audios de recette. Le groupe `10001`
correspond au groupe non privilégié du simulateur :

```bash
sudo install -d -o root -g 10001 -m 0750 /srv/gta-staging/audio-fixtures
```

Valider la chaîne TLS de GLPI sans jamais désactiver la vérification :

```bash
openssl s_client -connect NOM_GLPI:443 -servername NOM_GLPI -CAfile /chemin/ca.pem -verify_return_error </dev/null
```

Le résultat attendu est `Verify return code: 0 (ok)`. Pour PostgreSQL distant,
`DATABASE_URL` doit utiliser `sslmode=verify-full` et un nom couvert par le
certificat. `CA_BUNDLE_HOST_PATH` configure la confiance envers le serveur ; il
ne fournit pas un éventuel certificat client mTLS.

## Configuration des projets

Créer trois projets Compose Dokploy distincts :

| Ordre | Projet | Fichier Compose | Modèle d'environnement |
| ---: | --- | --- | --- |
| 1 | Simulateurs | `infra/docker/providers/docker-compose.staging.yml` | `infra/docker/providers/staging.env.example` |
| 2 | Transcription | `infra/docker/transcription/docker-compose.transcription.yml` | `infra/docker/transcription/transcription.env.example` |
| 3 | Applications | `infra/docker/apps/docker-compose.staging.yml` | `infra/docker/apps/staging.env.example` |

Copier chaque modèle dans l'onglet **Environment** du projet concerné, puis
remplacer tous les placeholders. Ne jamais copier une valeur réelle dans le
dépôt, une capture d'écran ou un ticket de support.

Contrôler en particulier les invariants suivants :

- `APP_ENVIRONMENT=staging` et `APP_COMMIT_SHA` contient le SHA complet
  réellement construit ;
- `SIMULATORS_NETWORK_NAME` est identique dans les projets simulateurs et
  applications ;
- `TRANSCRIPTION_NETWORK_NAME` est identique dans les projets transcription et
  applications ;
- `TRANSCRIPTION_API_KEY` est identique dans ces deux derniers projets ;
- `TRANSCRIBE_AI_SIMULATOR_API_KEY` est identique dans les projets simulateurs
  et applications si le repli legacy doit rester testable ;
- `MICROSOFT_GRAPH_SIMULATOR_AUDIO_FIXTURES_HOST_PATH` désigne le répertoire
  préparé sur la VM ;
- `DATABASE_URL` cible exclusivement la base de staging ;
- `GLPI_BASE_URL` et les identifiants associés ciblent exclusivement le GLPI de
  test ;
- `GLPI_TICKET_REQUEST_SOURCE_ID` correspond à la source `MEVO` de cette
  instance, sans recopier l'identifiant de production ;
- `CA_BUNDLE_HOST_PATH` est lisible par l'UID/GID `10001` ;
- `PUBLIC_BASE_URL` désigne le domaine interne du web staging.

Le provider nominal de staging est `openai_compatible`. Le simulateur
Transcribe AI reste disponible pour une bascule legacy explicite ; aucun
basculement automatique n'est réalisé.

## Déploiement

### 1. Simulateurs

Déployer la stack des simulateurs, puis attendre que
`microsoft-graph-simulator` et `transcribe-ai-simulator` soient `healthy`.
Aucun de leurs ports ne doit être publié vers l'hôte ou Traefik.

Dans le terminal du simulateur Microsoft Graph, vérifier l'interface
d'administration :

```bash
glpi-ticketing-microsoft-graph-simulator-admin list-scenarios
```

Dans le terminal du simulateur Transcribe AI, vérifier le contrat legacy :

```bash
glpi-ticketing-transcribe-ai-simulator-smoke
```

### 2. Transcription

Déployer la stack de transcription et attendre que `transcription-api` soit
`healthy`. Le service doit rester uniquement sur le réseau privé de
transcription, sans domaine Dokploy ni publication Traefik.

Depuis le terminal de `worker-automation` après son démarrage, sa sonde pourra
être contrôlée avec :

```bash
python -c 'import httpx; print(httpx.get("http://gta-staging-transcription:5092/health/ready", timeout=5).json())'
```

### 3. Applications

Déployer la stack applicative. Les projets Compose ne pouvant pas exprimer de
`depends_on` entre eux, les deux stacks précédentes doivent déjà être saines.

Vérifier ensuite que :

1. `database-migrations` termine avec le code `0` ;
2. `web`, `worker-publisher`, `worker-automation` et `worker-retention`
   démarrent ;
3. le dashboard affiche les trois workers avec des heartbeats frais ;
4. l'environnement, la release et le SHA affichés correspondent au déploiement ;
5. aucun journal ne contient de secret, d'audio, de transcription ou de
   réponse HTTP brute.

Seul le service `web`, sur son port interne `8000`, reçoit un domaine Dokploy.
Sans domaine fonctionnel, les workers peuvent être qualifiés mais les liens
audio présents dans GLPI ne sont pas utilisables.

## Recette fonctionnelle

### Parcours nominal synthétique

Injecter d'abord un message WAV depuis le terminal du simulateur Microsoft :

```bash
glpi-ticketing-microsoft-graph-simulator-admin seed --scenario valid-wav
```

Contrôler successivement :

1. la création de l'ingestion et du job ;
2. les transitions `pending`, `processing`, puis `completed` ;
3. le déplacement du message simulé dans `Processed` ;
4. la création du ticket dans le GLPI de test ;
5. la correspondance entre l'UUID du job et `external_id` dans GLPI ;
6. l'identifiant GLPI et les événements expurgés dans le dashboard ;
7. le téléchargement de l'audio fictif lorsque le domaine est disponible.

Rejouer ensuite le parcours avec OGG :

```bash
glpi-ticketing-microsoft-graph-simulator-admin seed --scenario valid-ogg
```

Le catalogue des rejets contrôlés est disponible avec :

```bash
glpi-ticketing-microsoft-graph-simulator-admin list-scenarios
```

Au minimum, vérifier qu'un message sans audio, avec plusieurs audios, avec un
audio vide ou avec un format non pris en charge est classé dans `Rejected`, que
son motif expurgé est persisté et que le publisher continue à traiter la file.

### Audio fictif fourni par l'opérateur

Installer un WAV ou un OGG fictif de moins de 25 Mio sur la VM :

```bash
sudo install -o root -g 10001 -m 0640 ./test-vocal.wav /srv/gta-staging/audio-fixtures/test-vocal.wav
```

Puis l'injecter depuis le terminal du simulateur :

```bash
glpi-ticketing-microsoft-graph-simulator-admin seed --audio-path /fixtures/audio/test-vocal.wav
```

Cette recette qualifie la détection du français, la transcription de messages
courts ou bruités et les limites CPU/mémoire. Le fichier monté demeure sur la
VM et doit être retiré manuellement après la recette.

### Rétention facultative

Sur cet environnement isolé, passer un ticket de test à `SOLVED` et confirmer
que l'audio reste disponible après un contrôle. Le passer ensuite à `CLOSED`,
attendre le contrôle suivant et confirmer la suppression des octets ainsi que
la réponse `410 Gone` au téléchargement.

## Diagnostic

Identifier d'abord la frontière défaillante :

1. découverte et acquittement dans le simulateur Graph ;
2. persistance PostgreSQL et publication sur le volume audio ;
3. provider de transcription ;
4. création ou consultation GLPI ;
5. rétention ;
6. dashboard et domaine Dokploy.

Les logs doivent fournir uniquement les UUID, étapes, catégories et codes
techniques expurgés. Ne pas publier la sortie complète de
`docker compose config`, qui peut résoudre des chemins et des valeurs
d'environnement. Ne jamais lancer de nettoyage Docker global pour diagnostiquer
cet environnement.

Les limites connues de la recette sont les suivantes :

- le simulateur Graph ne valide pas Microsoft Entra ID ;
- la qualité de Parakeet Ultra doit être confirmée sur des audios fictifs
  représentatifs ;
- le staging dépend du DNS, du réseau et des CA de la VM ;
- le rollback applicatif ne rétrograde pas automatiquement PostgreSQL ;
- aucune opération de nettoyage ne doit viser le GLPI de test externe.

## Nettoyage et retour arrière

Réinitialiser uniquement la boîte synthétique avec :

```bash
glpi-ticketing-microsoft-graph-simulator-admin reset
```

Cette commande ne supprime pas les fichiers du répertoire d'audios de recette.
Retirer manuellement les fichiers fictifs devenus inutiles. Ne jamais utiliser
`docker compose down -v` : le volume audio et les données PostgreSQL doivent
rester disponibles pour l'analyse ou un retour arrière.

En cas d'échec de déploiement, arrêter les workers avant toute restauration et
appliquer la [procédure de mise à jour](mise-a-jour.md). Ne pas rétrograder une
migration Alembic sans procédure explicitement validée.

## Résultat attendu

Le staging est valide lorsque les trois stacks sont saines, que la migration
est terminée, que les trois workers ont un heartbeat frais et que les parcours
WAV et OGG créent chacun un ticket corrélé dans le GLPI de test. Aucun service
de production, secret réel ou donnée sensible ne doit avoir été utilisé.
