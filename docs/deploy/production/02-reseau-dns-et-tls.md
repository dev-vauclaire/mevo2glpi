# 2. Préparer le réseau, le DNS et TLS

Les conteneurs utilisent la résolution DNS et la sortie réseau de la VM. Les
pare-feu peuvent donc voir l'adresse de la VM comme source des appels émis par
les workers.

## Matrice DNS

| Nom | Résolution requise depuis | Usage |
| --- | --- | --- |
| nom du dashboard | postes opérateurs et administrateurs | accès HTTPS au web |
| nom du GLPI | VM de production | API GLPI V2 |
| nom de Keycloak | VM et postes opérateurs | découverte et parcours OIDC |
| `login.microsoftonline.com` | VM | jeton applicatif Entra ID |
| `graph.microsoft.com` | VM | messages Exchange Online |
| `github.com` et `api.github.com` si nécessaire | VM | dépôt et intégration GitHub |
| `auth.docker.io`, `registry-1.docker.io`, `production.cloudfront.docker.com`, `ghcr.io`, `pkg-containers.githubusercontent.com` | VM | images Docker |
| `huggingface.co`, `hf.co` et leurs sous-domaines | VM, si Parakeet local | modèle épinglé |

## Matrice des flux

| Source | Destination | Protocole | Port | Usage |
| --- | --- | --- | ---: | --- |
| opérateurs | dashboard / Traefik | TCP | 443 | dashboard et audios autorisés |
| opérateurs | dashboard / Traefik | TCP | 80 | redirection vers HTTPS uniquement |
| opérateurs | Keycloak externe | TCP | 443 | redirection et authentification OIDC |
| réseau d'administration | VM | TCP | 22 | SSH par clé |
| réseau d'administration | Dokploy | TCP | 3000 | administration restreinte |
| VM | DNS établissement | UDP/TCP | 53 | résolution de la VM et des conteneurs |
| VM | NTP établissement | UDP | 123 | synchronisation système si nécessaire |
| VM | GitHub, registres et éventuellement Hugging Face | TCP | 443 | build et déploiement |
| VM | Entra ID et Microsoft Graph | TCP | 443 | ingestion Exchange |
| VM | Keycloak externe | TCP | 443 | authentification OIDC |
| VM | GLPI | TCP | 443 | création et suivi des tickets |
| VM | provider externe de transcription | TCP | port HTTPS validé | transcription externe uniquement |

Refuser les autres flux. Ne publier ni PostgreSQL, ni les workers, ni la
transcription locale sur l'hôte ou dans Traefik.

## Installer la confiance envers les services internes

Pour une autorité interne, installer la chaîne de confiance sur la VM :

```bash
sudo install -m 0644 /chemin/ca-interne.pem \
  /usr/local/share/ca-certificates/mevo2glpi-internal-ca.crt
sudo update-ca-certificates
```

Le fichier source contient la CA racine et, si nécessaire, les intermédiaires ;
il ne contient jamais une clé privée. Tester chaque service interne, par
exemple GLPI :

```bash
openssl s_client \
  -connect NOM_GLPI_PRODUCTION:443 \
  -servername NOM_GLPI_PRODUCTION \
  -CAfile /etc/ssl/certs/ca-certificates.crt \
  -verify_return_error </dev/null
```

Le résultat attendu est `Verify return code: 0 (ok)`.

Le bundle monté dans les conteneurs sera référencé par :

```dotenv
CA_BUNDLE_HOST_PATH=/etc/ssl/certs/ca-certificates.crt
```

Ne jamais contourner une erreur en désactivant la vérification TLS.

## Certificat du dashboard

Importer dans Dokploy le certificat serveur, sa chaîne complète et sa clé
privée. Le SAN doit couvrir exactement le nom DNS du dashboard. Sélectionner ce
certificat sur le domaine du service `web` ; sa clé reste gérée par
Dokploy/Traefik et n'est jamais montée dans les applications.

## Créer les réseaux privés

```bash
sudo docker network create --driver bridge mevo2glpi-prod-database-network
sudo docker network create --driver bridge --internal \
  mevo2glpi-prod-transcription-network
```

La recette applicative référence également le second réseau lorsqu'un provider
externe est utilisé : créer les deux réseaux dans tous les cas. Seul le service
Parakeet local rejoint effectivement le réseau de transcription. Si un réseau
existe déjà, vérifier son nom et ses propriétés au lieu de le recréer.

## Porte de validation

- [ ] tous les noms sont résolus depuis la VM ;
- [ ] les flux autorisés répondent, les services privés ne sont pas publiés ;
- [ ] les chaînes TLS internes sont validées sans option insecure ;
- [ ] le certificat du dashboard correspond au nom DNS ;
- [ ] le réseau PostgreSQL existe ;
- [ ] le réseau de transcription existe si Parakeet est local.

Étape suivante : [Exchange Online et Entra ID](03-exchange-online-et-entra-id.md).
