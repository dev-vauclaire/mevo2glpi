# Procédure de déploiement initial en production

## Objectif et périmètre

Cette procédure décrit la création d'un nouvel environnement de production
Dokploy pour GLPI Ticketing Automation. Elle ne couvre pas la promotion d'une
nouvelle version sur une installation existante, décrite dans la
[procédure de mise à jour](mise-a-jour.md).

La production utilise :

- une base PostgreSQL externe et dédiée ;
- un volume Docker persistant pour les audios ;
- un GLPI et une boîte Exchange Online de production ;
- soit le provider historique `legacy_polling`, soit la stack locale
  `openai_compatible` ;
- le Compose applicatif
  `infra/docker/apps/docker-compose.prod.yml`.

Les secrets, certificats, audios et données métier réels ne doivent jamais être
copiés dans Git, dans cette documentation ou dans une commande conservée dans
l'historique du terminal.

## Prérequis

Avant toute création de service, disposer des éléments suivants :

1. un tag immuable `vX.Y.Z` validé en staging et son SHA Git complet ;
2. une VM dimensionnée, à jour et équipée de Docker et Dokploy ;
3. une base, un rôle et une stratégie de sauvegarde PostgreSQL dédiés ;
4. une URL GLPI V2.3.0 directe, un utilisateur de service, un client OAuth et
   l'identifiant local de la source de demande `MEVO` ;
5. une application Entra ID autorisée uniquement sur la boîte dédiée, avec sa
   clé privée PEM et son certificat X.509 public PEM ;
6. un bundle de CA validant PostgreSQL, GLPI, Microsoft et le provider externe
   éventuel ;
7. un nom DNS interne pour le dashboard, routé par Traefik vers le port `8000`
   du service `web` ;
8. le choix explicite du provider de transcription.

L'accès web ne possède pas encore d'authentification applicative. Le domaine
doit rester limité au réseau interne par le DNS, le pare-feu et la
configuration Traefik.

## Préparer les ressources persistantes

Créer les réseaux externes attendus par Compose avec les noms retenus dans
l'environnement Dokploy. Les valeurs par défaut de production sont :

```bash
sudo docker network create --driver bridge gta-database-network
sudo docker network create --driver bridge --internal gta-prod-transcription-network
```

Le réseau de base doit permettre aux workers d'atteindre leurs dépendances
HTTPS. Le réseau de transcription peut rester `internal` et ne doit pas être
relié à Traefik. `dokploy-network` est normalement créé par Dokploy.

Raccorder PostgreSQL à `gta-database-network` lorsqu'il est déployé sur le même
hôte. Pour une base distante, conserver malgré tout le réseau externe déclaré
par Compose et utiliser dans `DATABASE_URL` un nom DNS résolu depuis les
conteneurs.

Le volume audio nommé est créé par Compose à partir de `AUDIO_VOLUME_NAME`.
Choisir un nom propre à la production et l'inclure dans la politique de
sauvegarde. Ne jamais exécuter `docker compose down -v` sur cette stack.

## Installer les certificats sur l'hôte

Installer sur la VM :

- un bundle CA combinant les autorités internes nécessaires et les autorités
  publiques utilisées par les dépendances ;
- la clé privée PEM Entra ;
- le certificat public X.509 PEM associé.

Les trois fichiers sont montés en lecture seule. La clé privée doit être
accessible uniquement aux administrateurs et à l'UID/GID `10001` utilisé dans
le conteneur publisher. Vérifier les métadonnées sans afficher leur contenu :

```bash
sudo stat -c '%n uid=%u gid=%g mode=%a' /chemin/ca-bundle.pem
sudo stat -c '%n uid=%u gid=%g mode=%a' /chemin/publisher-private-key.pem
sudo stat -c '%n uid=%u gid=%g mode=%a' /chemin/publisher-certificate.pem
```

Valider séparément la chaîne TLS des services internes avec
`openssl s_client`, sans désactiver la vérification. Une CA cliente mTLS ne peut pas être
remplacée par `CA_BUNDLE_HOST_PATH` : cette variable établit uniquement la
confiance envers le certificat présenté par le serveur.

## Déployer la transcription locale, si elle est sélectionnée

Pour `TRANSCRIPTION_PROVIDER=openai_compatible`, créer d'abord un projet
Dokploy basé sur :

```text
./infra/docker/transcription/docker-compose.transcription.yml
```

Reprendre `infra/docker/transcription/transcription.env.example` et adapter au
minimum :

- `COMPOSE_PROJECT_NAME=gta-prod-transcription` ;
- `APP_RELEASE=vX.Y.Z` ;
- `APP_COMMIT_SHA` avec le SHA complet du tag ;
- `TRANSCRIPTION_NETWORK_NAME=gta-prod-transcription-network` ;
- `TRANSCRIPTION_NETWORK_ALIAS=gta-prod-transcription` ;
- une nouvelle `TRANSCRIPTION_API_KEY` propre à la production ;
- les limites CPU et mémoire validées en staging.

Déployer puis attendre l'état `healthy`. Ne créer ni domaine Dokploy, ni port
publié pour ce service.

Avec `legacy_polling`, ne pas déployer cette stack. Le réseau de transcription
déclaré par le Compose applicatif doit néanmoins exister.

## Configurer le projet applicatif

Créer un projet Docker Compose Dokploy utilisant :

```text
./infra/docker/apps/docker-compose.prod.yml
```

Copier les variables de `infra/docker/apps/.env.example` dans l'onglet
Environment, puis remplacer tous les placeholders. Contrôler en particulier :

- `APP_ENVIRONMENT=production` ;
- `APP_RELEASE` contient le tag exact et `APP_COMMIT_SHA` son SHA complet ;
- `DATABASE_URL` cible la base dédiée et utilise `sslmode=verify-full` lorsque
  la connexion PostgreSQL passe par TLS ;
- les noms des réseaux et du volume sont propres à la production ;
- `CA_BUNDLE_HOST_PATH` et les deux chemins PEM désignent les fichiers préparés
  sur la VM ;
- `PUBLIC_BASE_URL` correspond au domaine interne du dashboard ;
- les identifiants Entra ciblent uniquement la boîte dédiée ;
- les identifiants GLPI et `GLPI_TICKET_REQUEST_SOURCE_ID` appartiennent à
  l'instance de production ;
- les secrets de staging ne sont jamais réutilisés.

Pour `legacy_polling`, renseigner son URL et sa clé. Pour
`openai_compatible`, utiliser la base
`http://gta-prod-transcription:5092/v1`, sélectionner le modèle attendu et
recopier exactement la clé de la stack locale. Ne renseigner dans les logs ou
la documentation aucune de ces valeurs.

Avant le déploiement, vérifier le rendu Compose avec un fichier temporaire
contenant uniquement les valeurs de l'environnement concerné :

```bash
docker compose --env-file /chemin/securise/production.env --file infra/docker/apps/docker-compose.prod.yml config --quiet
```

Ne jamais publier la sortie complète de `docker compose config`, car elle peut
contenir des valeurs résolues sensibles.

## Déployer les applications

Déclencher le déploiement du projet applicatif. L'ordre interne attendu est :

1. `database-migrations` attend PostgreSQL et exécute `alembic upgrade head` ;
2. la migration se termine avec le code `0` ;
3. le web et les trois workers démarrent après ce succès.

Si la migration échoue, ne démarrer manuellement aucun worker. Corriger la
cause, contrôler l'état de la base et relancer uniquement après une décision
explicite.

Dans Dokploy, associer le domaine interne uniquement au service `web` sur son
port conteneur `8000`.

## Vérifications

Contrôler successivement :

1. le code de sortie `0` de `database-migrations` ;
2. l'état démarré des quatre services persistants ;
3. `GET /health` sur le domaine interne ;
4. l'environnement, le tag et le SHA affichés par le dashboard ou
   `GET /api/version` ;
5. la présence d'un heartbeat frais pour chacun des trois workers ;
6. l'absence de secret, transcription, audio ou réponse HTTP brute dans les
   journaux ;
7. l'absence d'exposition Traefik de PostgreSQL et de la transcription locale.

Effectuer ensuite un test fonctionnel autorisé avec un message et un audio
entièrement fictifs. Vérifier le déplacement Exchange vers `Processed`, la
transcription, la création d'un unique ticket GLPI, la corrélation de son UUID
et l'accès contrôlé à l'audio depuis le dashboard.

## Échec initial et retour arrière

En cas d'échec avant le premier traitement métier :

1. arrêter les trois workers ;
2. conserver les journaux nécessaires au diagnostic sans les diffuser ;
3. ne supprimer ni le volume audio, ni la base PostgreSQL ;
4. corriger la configuration ou redéployer le même tag ;
5. restaurer uniquement une sauvegarde cohérente de PostgreSQL et du volume si
   une corruption est démontrée.

Dès que l'environnement a traité des messages, utiliser la
[procédure de mise à jour](mise-a-jour.md). Un retour arrière applicatif ne
réalise jamais automatiquement le downgrade du schéma Alembic.

## Résultat attendu

L'environnement affiche le tag et le SHA prévus, les migrations sont à jour,
les trois workers ont un heartbeat frais, le dashboard est accessible
uniquement depuis le réseau interne et un message synthétique traverse le flux
sans créer de doublon ni exposer de donnée sensible.
