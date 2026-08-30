# Runbook de démonstration et de recette du MVP

Ce runbook décrit la preuve fonctionnelle du MVP avant déploiement. Il ne
remplace pas le contrat de déploiement Dokploy décrit dans le
[`README`](../README.md#déploiement-dokploy-du-mvp).

## Périmètre démontré

La recette utilise les vraies images du web, des deux workers et de la
migration. PostgreSQL, GLPI 11 et Transcribe AI sont jetables. Le web est
joignable uniquement sur loopback pendant le test ; Traefik reste une
responsabilité de Dokploy. Les identifiants, réseaux, volumes et bases sont
propres à la session.

Elle prouve les invariants suivants :

| Parcours | Preuve attendue |
|---|---|
| Lot nominal | Trois WAV envoyés simultanément deviennent `completed`, avec événements ordonnés, audio identique et un ticket GLPI corrélé par UUID. |
| Erreur de stockage | Une taille audio incohérente produit `blocked` et une alerte ; après réparation, le retry opérateur termine le job. |
| Erreur terminale | Un job `failed` et son alerte sont visibles ; le retry opérateur réarme le même UUID. |
| Panne Transcribe AI | Un seul job porte le retry système et les jobs suivants restent `pending` ; tous repartent après rétablissement. |
| Panne GLPI | Le job reste récupérable, redemande une transcription et ne crée qu'un ticket. |
| Crash du worker | Un `processing` abandonné est repris après `SIGKILL` et redémarrage, sans doublon GLPI. |
| Rétention GLPI | `SOLVED` conserve l'audio ; `CLOSED` le purge et le téléchargement répond `410 Gone`. |
| Deadline | Une échéance dépassée purge l'audio même lorsque GLPI est indisponible. |

Le contrat de la fixture Transcribe AI vérifie séparément les quatre formats
acceptés par le produit : WAV, MP3, OGG et M4A. Le scénario E2E nominal utilise
un WAV synthétique unique afin de limiter le coût CPU et de rendre la comparaison
des octets déterministe.

## Prérequis

- Docker Engine et Docker Compose fonctionnels ;
- `uv` installé ;
- capacité à réserver deux ports loopback éphémères pour le web et PostgreSQL ;
- capacité à télécharger ou à réutiliser les images épinglées et le modèle
  Whisper `tiny` ;
- mémoire et espace disque suffisants pour GLPI, MySQL, PostgreSQL et
  Transcribe AI ;
- aucune autre suite Docker de ce dépôt en cours d'exécution.

La validation de référence est obligatoirement CPU et séquentielle :

```bash
TRANSCRIBE_AI_TEST_DEVICE=cpu \
uv run pytest -m e2e tests/e2e -vv --maxfail=1
```

Le résultat attendu est `9 passed`. N'utilisez ni `pytest-xdist`, ni `-n`.
Une validation GPU peut compléter cette preuve, mais ne la remplace pas :

```bash
TRANSCRIBE_AI_TEST_DEVICE=gpu \
uv run pytest -m e2e tests/e2e -vv --maxfail=1
```

Les fixtures attendent chaque service par une sonde bornée et détruisent les
ressources créées, y compris après un échec. Elles ne nettoient jamais les
autres projets du daemon Docker.

## Démonstration manuelle sur un environnement isolé

1. Déployer la stack avec une base PostgreSQL et un volume audio neufs selon le
   guide Docker.
2. Ouvrir l'URL HTTPS configurée par Dokploy sur le réseau interne.
3. Déposer jusqu'à trois fichiers parmi WAV, MP3, OGG et M4A. Vérifier que
   chaque dépôt reçoit son propre UUID et que l'interface permet immédiatement
   un nouveau dépôt.
4. Suivre les transitions `pending`, `processing`, puis `completed` dans le
   tableau. Ouvrir le détail et vérifier l'historique d'événements.
5. Dans GLPI, contrôler que le ticket contient l'UUID dans `external_id`, la
   transcription et le lien HTTPS vers l'audio. L'ID GLPI affiché dans le
   dashboard doit correspondre.
6. Télécharger l'audio terminé depuis le dashboard.
7. Facultatif, sur cet environnement isolé uniquement : passer le ticket à
   `SOLVED`, attendre l'intervalle de contrôle configuré et confirmer que
   l'audio reste disponible ; passer ensuite le ticket à `CLOSED`, attendre le
   contrôle suivant et confirmer sa suppression et la réponse `410 Gone`.

Les injections de panne et les changements artificiels d'horodatage sont
réservés à la suite jetable. Ne stoppez jamais GLPI ou Transcribe AI de
production pour une démonstration.

## Diagnostic borné

En cas d'échec, identifier d'abord la frontière concernée : bootstrap de stack,
pipeline métier, fournisseur ou rétention. Les messages pytest sont expurgés :
ils donnent le statut, l'UUID ou le type d'événement utile sans afficher de clé,
de réponse HTTP brute ni de transcription.

Ne publiez pas la sortie complète de `docker compose config`, car elle peut
résoudre des chemins et des valeurs d'environnement sensibles. Ne lancez jamais
de nettoyage Docker global ; les fixtures ne possèdent que les ressources dont
le nom de projet est généré pour leur session.

## Limites résiduelles du MVP

- un seul worker d'automatisation et un seul worker de rétention sont supportés ;
- Transcribe AI v1 n'offre pas d'idempotence distante ni de conservation
  garantie : un retry ou un crash redemande une transcription et peut laisser un
  job fournisseur orphelin ;
- la transcription reste uniquement en mémoire ;
- l'auto-restart Docker est testé séparément par un processus qui échoue
  naturellement, tandis que l'E2E redémarre explicitement le conteneur après le
  `SIGKILL` afin d'isoler la preuve de reprise métier ;
- les alertes sont consultables sur le dashboard mais ne sont pas encore
  transmises à un système de notification externe ;
- l'interface reste limitée au réseau interne ; une exposition Internet exige
  un contrôle d'accès configuré au niveau de Dokploy/Traefik ;
- la recette appelle FastAPI directement en HTTP sur loopback : elle ne valide
  ni Traefik, ni le certificat TLS, ni un navigateur et son JavaScript ;
- le pilotage temporel E2E ne change que des timestamps autorisés sur une
  rétention déjà `scheduled` ; il ne fabrique aucune transition métier en SQL ;
- le déploiement Dokploy construit les images sur le serveur ; il ne fournit
  pas encore de promotion par digest ni de signature d'image.
