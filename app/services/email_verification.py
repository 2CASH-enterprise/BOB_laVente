"""
Lot 41 — vérification de l'email à l'inscription (et à la transformation d'une démo en compte).

- Un code à 6 chiffres est envoyé à l'adresse ; seule son empreinte est gardée en base.
- Valable 10 minutes, 5 essais au plus ; un nouveau code remplace l'ancien.
- Le compte n'est créé qu'avec le bon code : une faute de frappe dans l'email (alertes perdues,
  mot de passe impossible à récupérer) est repérée tout de suite.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.models.email_verification import EmailVerification
from app.services.otp_service import generate_otp, hash_otp, verify_otp

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
INVALID_CODE = "Code incorrect ou expiré. Vérifiez le dernier email reçu, ou demandez un nouveau code."


def normalize(email: str) -> str:
    return (email or "").strip().lower()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def new_code(db, email: str, now: datetime | None = None) -> str:
    """Crée (et renvoie, pour l'email) un nouveau code ; les précédents sont effacés."""
    email = normalize(email)
    await db.execute(delete(EmailVerification).where(EmailVerification.email == email))
    code = generate_otp()
    db.add(EmailVerification(email=email, code_hash=hash_otp(code),
                             expires_at=(now or datetime.now(timezone.utc)) + CODE_TTL))
    return code


async def check_code(db, email: str, code: str | None, now: datetime | None = None) -> bool:
    """True si le code est bon (et le consomme). Sinon compte l'essai ; l'appelant doit commiter."""
    email = normalize(email)
    now = now or datetime.now(timezone.utc)
    row = (await db.execute(select(EmailVerification).where(EmailVerification.email == email)
                            .order_by(EmailVerification.created_at.desc()).limit(1))).scalar_one_or_none()
    if row is None or _aware(row.expires_at) <= now or row.attempts >= MAX_ATTEMPTS:
        return False
    if not code or not code.isdigit() or len(code) != 6 or not verify_otp(code, row.code_hash):
        row.attempts += 1
        return False
    await db.execute(delete(EmailVerification).where(EmailVerification.email == email))
    return True


def code_email(code: str) -> tuple[str, str]:
    return (
        "Votre code pour créer votre compte Bob",
        "Bonjour,\n\nVoici votre code pour terminer la création de votre compte Bob :\n\n"
        f"{code}\n\nIl est valable 10 minutes. Si vous n'avez pas demandé à créer un compte Bob, ignorez simplement cet email.",
    )
