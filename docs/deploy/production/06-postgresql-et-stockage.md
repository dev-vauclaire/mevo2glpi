# 6. Créer PostgreSQL, le volume et les secrets hôte

## PostgreSQL dans Dokploy

Créer une ressource **Database PostgreSQL** dans Dokploy, avec un volume
persistant, sans domaine ni port public. L'attacher au réseau externe :

```text
mevo2glpi-prod-database-network
```

Utiliser la base, l'utilisateur et le mot de passe configurés dans cette
ressource Dokploy. Le nom DNS utilisé dans `DATABASE_URL` est le nom réellement
attribué au service sur ce réseau ; il peut différer de `postgres`.

```dotenv
DATABASE_URL=postgresql+psycopg://UTILISATEUR:MOT_DE_PASSE_ENCODE@NOM_SERVICE:5432/BASE
DATABASE_CONNECT_TIMEOUT_SECONDS=5
DATABASE_READY_TIMEOUT_SECONDS=60
DATABASE_READY_INTERVAL_SECONDS=1
```

Encoder les caractères réservés du mot de passe dans l'URL. `localhost`
désignerait le conteneur appelant et ne doit pas être utilisé.

## Volume audio

Le projet applicatif crée et réutilise le volume nommé :

```dotenv
AUDIO_VOLUME_NAME=mevo2glpi-prod-audio-data
```

Le publisher et la rétention y écrivent ; le web et l'automatisation le montent
en lecture seule. Ne pas créer un nouveau nom lors d'une mise à jour, sous peine
de rendre les audios existants invisibles.

## Certificat Entra du publisher

Installer sur l'hôte le certificat X.509 public et sa clé privée associée :

```bash
sudo install -d -m 0750 -o root -g 10001 /etc/mevo2glpi/secrets
sudo install -m 0640 -o root -g 10001 /chemin/cle-privee.pem \
  /etc/mevo2glpi/secrets/publisher-private-key.pem
sudo install -m 0640 -o root -g 10001 /chemin/certificat-public.pem \
  /etc/mevo2glpi/secrets/publisher-certificate.pem
```

Le certificat doit contenir `BEGIN CERTIFICATE`, pas seulement
`BEGIN PUBLIC KEY`. Vérifier l'association sans afficher la clé :

```bash
sudo openssl pkey \
  -in /etc/mevo2glpi/secrets/publisher-private-key.pem \
  -pubout -outform PEM | sha256sum
sudo openssl x509 \
  -in /etc/mevo2glpi/secrets/publisher-certificate.pem \
  -pubkey -noout | sha256sum
```

Les deux empreintes doivent être identiques.

## Clé de protection du numéro d'origine

Créer une clé dédiée hors Git et la conserver tant que des jobs sont en cours :

```bash
sudo sh -c 'umask 027; openssl rand -base64 32 > /etc/mevo2glpi/secrets/originating-phone-number.key'
sudo chown root:10001 /etc/mevo2glpi/secrets/originating-phone-number.key
sudo chmod 0640 /etc/mevo2glpi/secrets/originating-phone-number.key
```

Vérifier uniquement les métadonnées :

```bash
sudo stat -c '%U %G %a %n' /etc/mevo2glpi/secrets/*
```

La configuration finale référence :

```dotenv
PUBLISHER_ENTRA_PRIVATE_KEY_PEM_HOST_PATH=/etc/mevo2glpi/secrets/publisher-private-key.pem
PUBLISHER_ENTRA_CERTIFICATE_PEM_HOST_PATH=/etc/mevo2glpi/secrets/publisher-certificate.pem
ORIGINATING_PHONE_NUMBER_KEY_HOST_PATH=/etc/mevo2glpi/secrets/originating-phone-number.key
CA_BUNDLE_HOST_PATH=/etc/ssl/certs/ca-certificates.crt
```

## Sauvegardes

Le dépôt n'automatise ni PostgreSQL, ni le volume audio, ni les secrets hôte.
Leur sauvegarde relève de la politique de l'hyperviseur. Elle n'est pas une
étape bloquante de l'installation initiale, mais une restauration doit être
qualifiée avant de s'appuyer sur ce mécanisme pour une mise à jour.

## Porte de validation

- [ ] PostgreSQL est privé et attaché au bon réseau ;
- [ ] son nom DNS réel est utilisé dans `DATABASE_URL` ;
- [ ] le volume audio porte le nom définitif ;
- [ ] les fichiers sensibles appartiennent à `root:10001` et sont en `0640` ;
- [ ] le certificat Entra et la clé privée correspondent ;
- [ ] aucun secret n'est suivi par Git.

Étape suivante : [transcription](07-transcription.md).
