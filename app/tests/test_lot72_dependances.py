"""Lot 72 — dépendances à jour (audit pip-audit du 16/10) et jetons de connexion par PyJWT."""
import re
from pathlib import Path

from app.core import security

REQUIREMENTS = (Path(__file__).resolve().parents[2] / "requirements.txt").read_text(encoding="utf-8")


def _pin(name: str) -> tuple[int, ...]:
    match = re.search(rf"^{re.escape(name)}(?:\[[^\]]+\])?==([\d.]+)", REQUIREMENTS, re.M | re.I)
    assert match, name
    return tuple(int(x) for x in match.group(1).split("."))


def test_python_jose_is_gone_and_pyjwt_is_used():
    assert not re.search(r"^(python-jose|ecdsa)", REQUIREMENTS, re.M | re.I)
    assert security.jwt.__name__ == "jwt" and hasattr(security.jwt, "PyJWTError")


def test_versions_that_fix_the_known_flaws():
    assert _pin("starlette") >= (1, 3, 1)
    assert _pin("python-multipart") >= (0, 0, 31)
    assert _pin("fastapi") >= (0, 143, 0)
    assert _pin("pytest") >= (9, 0, 3)
    assert _pin("cryptography") >= (50, 0, 0)  # chiffrement Fernet : explicite depuis le départ de python-jose
    assert re.search(r"^sqlalchemy\[asyncio\]==", REQUIREMENTS, re.M)  # greenlet garanti


def test_tokens_issued_before_the_switch_stay_valid():
    """Un jeton HS256 standard (celui que python-jose produisait) se lit toujours : personne n'est déconnecté."""
    import base64
    import hashlib
    import hmac
    import json
    import time

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"sub": "u1", "tenant_id": "t1", "role": "OWNER", "exp": int(time.time()) + 60}).encode())
    key = security.settings.secret_key.encode()
    signature = b64(hmac.new(key, f"{header}.{body}".encode(), hashlib.sha256).digest())
    payload = security.jwt.decode(f"{header}.{body}.{signature}", security.settings.secret_key,
                                  algorithms=[security.settings.algorithm])
    assert payload["sub"] == "u1" and payload["role"] == "OWNER"
