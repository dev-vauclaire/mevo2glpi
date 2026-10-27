## Objectif

Décrire le problème traité et le résultat attendu, sans inclure de donnée réelle
ou sensible.

## Changements

-

## Validation

- [ ] `uv run ruff format --check .`
- [ ] `uv run ruff check .`
- [ ] tests ciblés adaptés au changement
- [ ] CI GitHub terminée avec succès

## Données, sécurité et exploitation

- [ ] aucun secret, fichier `.env`, certificat, audio réel ou donnée sensible
      n'est présent dans le diff ou les logs
- [ ] les entrées non fiables et les erreurs sont traitées sans fuite de données
- [ ] toute migration est additive et testée depuis une base neuve et la
      révision précédente
- [ ] la configuration, le déploiement et la documentation sont adaptés si
      leur contrat change
- [ ] les impacts sur GLPI, Exchange, Transcribe AI, PostgreSQL et le stockage
      audio sont explicités

## Déploiement et retour arrière

Indiquer « sans objet » ou décrire les vérifications staging, les éventuelles
opérations manuelles et la stratégie de retour arrière.
