# Runbook de démonstration et de recette du MVP

Ce runbook décrit une preuve fonctionnelle bornée avant déploiement. Il ne
remplace pas le contrat de déploiement Dokploy du [`README`](../README.md).
Utilisez uniquement des messages et audios synthétiques, sans donnée réelle de
patient.

## Périmètre démontré

Le parcours de production comporte la migration one-shot, le web en lecture
seule et trois workers : publisher, automation et retention. Le publisher lit
une boîte Exchange Online dédiée avec Microsoft Graph, publie le WAV ou l'OGG
et crée le job ; les deux autres workers transcrivent, créent le ticket et
appliquent sa rétention.

La recette E2E automatisée valide neuf scénarios aval :

| Parcours | Preuve attendue |
| --- | --- |
| Lot nominal | Trois sorties publisher synthétiques deviennent `completed`, avec événements ordonnés, audio identique et ticket GLPI corrélé par UUID. |
| Erreur de stockage | Une taille audio incohérente produit `blocked` et une alerte ; après réparation, le retry opérateur termine le job. |
| Erreur terminale | Un job `failed` et son alerte sont visibles ; le retry opérateur réarme le même UUID. |
| Panne du provider de transcription | Un seul job porte le retry système et les jobs suivants restent `pending` ; après trois reprogrammations par défaut, un ticket explicatif est créé. |
| Panne GLPI | Le job redemande une transcription pendant son budget de retry puis devient `blocked` avec son audio conservé ; aucun doublon de ticket n'est créé. |
| Crash automation | Un `processing` abandonné est repris après `SIGKILL`, sans doublon GLPI. |
| Rétention GLPI | `SOLVED` conserve l'audio ; `CLOSED` le purge et le téléchargement répond `410 Gone`. |
| Deadline | Une échéance dépassée purge l'audio même lorsque GLPI est indisponible. |

L'E2E ne contacte pas Microsoft 365 et ne démarre pas le publisher réel, car il
ne possède ni tenant Entra ID ni boîte Exchange de test. Un helper réservé aux
tests reproduit exclusivement sa frontière durable : publication atomique de
l'audio, création du job et événement `job_created` avec l'acteur
`worker_publisher`. Les contrats Graph, le streaming, les dossiers
`Processed`/`Rejected` et l'orchestration du publisher sont testés séparément
avec un faux serveur Graph stateful lié à `127.0.0.1`.

GLPI 11, MySQL, PostgreSQL et Transcribe AI sont jetables pendant l'E2E. Le web
est exposé uniquement sur loopback ; le Traefik et le TLS de Dokploy ne font pas
partie de cette recette.

## Prérequis de l'E2E

- Docker Engine et Docker Compose fonctionnels ;
- `uv` installé ;
- capacité à réserver des ports loopback éphémères ;
- capacité à télécharger ou réutiliser les images épinglées et le modèle
  Whisper `tiny` ;
- mémoire et espace disque suffisants pour les stacks jetables ;
- aucune autre suite Docker lourde de ce dépôt en cours d'exécution.

La validation de référence est CPU et strictement séquentielle :

```bash
TRANSCRIBE_AI_TEST_DEVICE=cpu \
uv run pytest -m e2e tests/e2e -vv --maxfail=1
```

Le résultat attendu est `9 passed`. N'utilisez ni `pytest-xdist`, ni `-n`.
Une validation GPU peut compléter la preuve, mais ne la remplace pas :

```bash
TRANSCRIBE_AI_TEST_DEVICE=gpu \
uv run pytest -m e2e tests/e2e -vv --maxfail=1
```

Les fixtures attendent chaque service avec une sonde bornée et détruisent les
ressources de leur session, y compris après un échec. Elles ne nettoient jamais
les autres projets du daemon Docker.

## Démonstration manuelle sur un environnement isolé

### Préparer l'environnement

1. Déployer la stack avec une base PostgreSQL et un volume audio neufs selon le
   guide Dokploy.
2. Vérifier que le dashboard affiche les trois workers avec un heartbeat frais.
3. Confirmer que l'identité applicative est limitée à la boîte dédiée. Les
   dossiers `Processed` et `Rejected` sont créés de manière idempotente au
   premier besoin ; aucune création manuelle n'est requise.
4. Ouvrir l'URL HTTPS du web depuis le réseau interne. L'interface ne doit
   proposer aucun dépôt de fichier.

### Parcours nominal

1. Envoyer vers la boîte dédiée jusqu'à trois messages de test. Chaque message
   doit contenir exactement un WAV ou OGG synthétique, non vide et inférieur ou
   égal à 25 Mio.
2. Vérifier que les messages quittent l'Inbox pour `Processed` après leur
   publication durable.
3. Dans le dashboard, suivre chaque job de `pending` à `processing`, puis
   `completed`. Le détail doit présenter les événements expurgés et l'UUID du
   job.
4. Dans GLPI, vérifier que le ticket contient le même UUID dans `external_id`,
   la transcription et le lien HTTPS vers l'audio. L'identifiant GLPI affiché
   dans le dashboard doit correspondre.
5. Télécharger l'audio terminé depuis le dashboard et vérifier qu'il correspond
   à l'audio synthétique envoyé.

### Rejet contrôlé

1. Envoyer un message synthétique sans audio WAV/OGG exploitable, avec un audio
   vide, corrompu ou supérieur à 25 Mio.
2. Vérifier que le message est déplacé dans `Rejected` seulement après
   l'enregistrement durable de son motif expurgé en base.
3. Vérifier que le publisher reste sain et continue de traiter les messages
   suivants. Le MVP ne fournit pas encore de liste web dédiée à ces ingestions
   rejetées.

### Rétention facultative

Sur cet environnement isolé uniquement, passer un ticket à `SOLVED`, attendre
le contrôle de rétention et confirmer que l'audio reste disponible. Passer
ensuite le ticket à `CLOSED`, attendre le contrôle suivant et confirmer la
suppression des octets et la réponse `410 Gone` au téléchargement.

Les injections de panne et les changements artificiels d'horodatage restent
réservés à la suite jetable. Ne stoppez jamais GLPI, Transcribe AI, Exchange ou
Microsoft Graph de production pour une démonstration.

## Diagnostic borné

Commencez par identifier la frontière en défaut :

- acquisition Entra ID ou Graph et ingestion publisher ;
- publication PostgreSQL/volume ;
- Transcribe AI ;
- création GLPI ;
- rétention ;
- dashboard.

Les logs donnent les UUID, étapes, catégories et codes techniques nécessaires
sans exposer de clé, de jeton, d'identifiant opaque de message, de réponse HTTP
brute ni de transcription. Ne publiez pas la sortie complète de
`docker compose config`, car elle peut résoudre des chemins et valeurs
d'environnement sensibles. Ne lancez jamais de nettoyage Docker global.

## Limites résiduelles

- une seule instance de chacun des trois workers est supportée ;
- le publisher fonctionne par polling et accepte exactement un WAV ou OGG par
  message ;
- les rejets d'ingestion sont persistés, mais n'ont pas de liste dédiée dans le
  dashboard ;
- Transcribe AI ne garantit ni idempotence ni conservation du résultat : un
  retry peut redemander une transcription et laisser un job distant orphelin ;
- les alertes sont visibles dans le dashboard sans notification externe ;
- le web ne possède pas d'authentification applicative et doit rester limité au
  réseau interne ;
- l'E2E ne valide ni un tenant Entra ID réel, ni Traefik, ni le certificat TLS,
  ni un navigateur et son JavaScript ;
- le déploiement construit les images sur le serveur, sans promotion par digest,
  signature d'image ou SBOM.
