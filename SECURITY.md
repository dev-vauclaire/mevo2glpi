# Politique de sécurité

## Signaler une vulnérabilité

Ne pas ouvrir d'issue GitHub publique pour une vulnérabilité, un secret exposé
ou un incident contenant des informations internes.

Utiliser le canal institutionnel privé convenu avec les mainteneurs du projet.
Si aucun canal n'a encore été communiqué à l'établissement évaluateur,
contacter son interlocuteur institutionnel habituel sans joindre de secret, de
preuve contenant une donnée réelle ou de clé privée. Un canal sécurisé sera
alors défini avant l'envoi des détails.

Le signalement initial peut contenir :

- la version ou le SHA concerné ;
- le composant et le scénario affectés ;
- l'impact estimé ;
- des étapes de reproduction avec des données exclusivement synthétiques ;
- les mesures temporaires déjà prises.

Aucun délai de prise en charge ou de correction n'est garanti publiquement à
ce stade. Les parties conviennent du niveau d'urgence et du mode de
coordination par le canal privé.

## Versions et périmètre

Les tags `vX.Y.Z` immuables sont les seules versions destinées au déploiement.
`main` peut contenir des changements non encore publiés. Les versions prises en
charge et leurs limites sont indiquées dans leurs notes de release et dans le
[guide d'adoption](docs/adoption.md).

La sécurité opérationnelle dépend également de la configuration locale :
segmentation réseau, TLS, secrets Dokploy, droits Entra/Exchange, droits GLPI,
sauvegardes de l'hyperviseur et politiques d'accès au dashboard.
