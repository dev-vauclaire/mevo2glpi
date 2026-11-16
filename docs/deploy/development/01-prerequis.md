# Étape 1 — Vérifier les prérequis

## Objectif

Vérifier que la machine peut exécuter le workspace Python et les dépendances
Docker du profil de développement choisi.

## Outils requis

- Git ;
- Python 3.12 ou supérieur ;
- uv ;
- Docker Engine ;
- Docker Compose v2 ;
- OpenSSL pour générer la clé locale du numéro d'origine ;
- curl pour les sondes HTTP.

Le dépôt utilise un unique `uv.lock`. Les dépendances des applications ne
doivent pas être installées séparément.

## Capacité indicative

Les mesures ci-dessous ont été réalisées sur un Intel Core i5-11400H doté de
6 cœurs physiques et 12 cœurs logiques, avec un seul worker et un seul
traitement simultané. Elles utilisent exclusivement les données synthétiques
du dépôt. L'E2E applicatif utilisait le simulateur OpenAI-compatible ; Parakeet
Q8 a été mesuré séparément à la frontière HTTP avec sa limite Compose de deux
vCPU et 4 Gio. Le pic du profil complet combine ces mesures de manière
conservatrice.

| Profil mesuré | RAM au repos | Pic RAM conservateur | CPU observé | Disque après construction à froid |
| --- | ---: | ---: | ---: | ---: |
| Dashboard et PostgreSQL | environ 140 Mio | moins de 200 Mio | moins de 1 vCPU | environ 700 Mio |
| Pipeline simulé avec GLPI | environ 1,15 Gio | environ 1,4 Gio | moins de 1 vCPU sur le scénario E2E | environ 6 Gio |
| Pipeline complet avec Parakeet CPU | environ 1,6 Gio | environ 2,5 Gio | 2 vCPU saturés pendant la transcription | environ 11 Gio, caches de construction inclus |

Le cas limite de 600 secondes accepté par l'API de transcription a été traité
en environ 79 secondes. Le conteneur Parakeet a alors atteint environ 1,1 Gio
de RAM et a utilisé ses deux vCPU pendant le traitement.

Pour conserver une marge pour Docker, les outils de développement et les
variations de charge, utiliser les capacités suivantes. La RAM indiquée doit
être libre avant le démarrage des services ; il ne s'agit pas de la RAM totale
de la machine.

| Profil | vCPU ou cœurs logiques recommandés | RAM libre recommandée | Disque libre recommandé |
| --- | ---: | ---: | ---: |
| Dashboard minimal | 2 vCPU | 2 Gio | 5 Gio |
| Pipeline simulé avec GLPI | 2 vCPU | 4 Gio | 10 Gio |
| Pipeline avec Parakeet CPU | 4 vCPU | 6 Gio | 15 Gio |

Le disque recommandé couvre les images, les volumes techniques et les caches
de construction, mais pas une conservation durable des audios. Cette capacité
supplémentaire se calcule ainsi :

```text
capacité_audio = audios_par_jour × taille_moyenne_audio × jours_de_rétention
```

`audios_par_jour` est le nombre moyen d'audios reçus chaque jour,
`taille_moyenne_audio` leur taille moyenne exprimée en Gio et
`jours_de_rétention` leur durée de conservation. Le résultat, exprimé en Gio,
doit être ajouté au disque libre recommandé.

Ces valeurs servent au développement et ne constituent pas un dimensionnement
de production. Les résultats restent propres au processeur mesuré et au
traitement séquentiel actuellement prévu par l'application ; les performances
et la latence peuvent varier sur une autre architecture CPU.

## Ports loopback par défaut

| Service | Port |
| --- | ---: |
| PostgreSQL applicatif | `5432` |
| Web mevo2glpi | `8000` |
| Microsoft Graph simulé | `18080` |
| Transcription simulée | `18082` |
| GLPI local | `18083` |
| Keycloak local | `18084` |

Tous les ports Docker de développement sont liés à `127.0.0.1`. Si un port est
occupé, sa variable `MEVO2GLPI_DEV_*_PORT` et l'URL applicative correspondante
doivent être modifiées ensemble.

## Vérifications

Depuis la racine du dépôt :

```bash
python --version
uv --version
docker version
docker compose version
git status --short --branch
```

Docker doit être accessible par l'utilisateur courant et le répertoire de
travail ne doit contenir aucun secret ou audio réel.

## Étape suivante

Continuer avec la [préparation de la configuration](02-configuration-locale.md).
