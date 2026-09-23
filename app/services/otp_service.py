"""Code à 6 chiffres pour la double authentification (2FA) — jamais stocké en clair."""
import hashlib
import secrets


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def verify_otp(code: str, hashed: str) -> bool:
    return hashlib.sha256(code.encode()).hexdigest() == hashed
