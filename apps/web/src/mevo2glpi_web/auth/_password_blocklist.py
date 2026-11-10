"""Petite blocklist locale versionnee pour les comptes applicatifs.

Les valeurs sont generiques et synthetiques. Ce fichier ne doit jamais
contenir de mot de passe observe dans un environnement reel.
"""

BLOCKED_PASSWORDS = frozenset(
    {
        "administrator",
        "change-me-please",
        "development-only",
        "mevo2glpi-local",
        "motdepasse-securise",
        "password-password",
        "welcome-to-mevo2glpi",
    }
)
