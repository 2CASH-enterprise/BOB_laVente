"""
Lot 37b — génère les clés des notifications (VAPID), UNE SEULE FOIS par installation.

Les lignes sont écrites sur la sortie standard pour être ajoutées directement au fichier .env,
sans jamais s'afficher à l'écran :

    docker compose exec -T api python -m app.scripts.generate_vapid_keys >> .env

Refuse de remplacer des clés déjà configurées : de nouvelles clés désactiveraient les
notifications de tous les appareils déjà abonnés (chacun devrait les réactiver).
"""
import base64
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    private = _b64(key.private_numbers().private_value.to_bytes(32, "big"))
    public = _b64(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    return public, private


def main() -> int:
    from app.core.config import get_settings

    if get_settings().vapid_private_key and "--force" not in sys.argv:
        print("Clés déjà configurées : rien n'a été changé.", file=sys.stderr)
        return 1
    public, private = generate()
    sys.stdout.write(f"\n# Lot 37b — notifications (ne jamais partager VAPID_PRIVATE_KEY)\n"
                     f"VAPID_PUBLIC_KEY={public}\nVAPID_PRIVATE_KEY={private}\n")
    print("Clés ajoutées.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
