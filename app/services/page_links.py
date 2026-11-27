"""
Lot 129 b — liens « une seule page » envoyés par Bob sur le numéro AgenC'AI (décisions de Thérèse du 23/11).

Le restaurateur touche « Ma carte », « Mes horaires » ou « Affiches et QR » dans le menu de Bob : il reçoit un lien
qui ouvre cette page, déjà connectée, sans le reste du tableau de bord. Le lien sert une seule fois, pendant
15 minutes (comme les liens de Naomy) ; il ouvre une session de 30 minutes qui ne peut appeler que les routes de la
page (vérifié par le serveur à chaque requête, comme la pré-inscription du lot 65). Seule l'empreinte du jeton est
gardée (lot 118). Jamais pour un compte en double vérification : Bob renvoie alors la page du tableau de bord, avec
la connexion habituelle. Le bouton « Tableau de bord » n'utilise jamais ce mécanisme (connexion habituelle).
"""
import re
from datetime import datetime, timedelta, timezone

VALID = timedelta(minutes=15)
SESSION_MINUTES = 30
PER_DAY = 30
PURPOSE_PREFIX = "PAGE_"
LOCKED_MESSAGE = "Cette page ouverte depuis WhatsApp ne donne accès qu'à elle-même. Pour le reste, ouvrez le tableau de bord."

_ID = r"[0-9a-fA-F-]{36}"
_COMMON = [("GET", r"/api/v1/tenants/me")]
PAGES: dict[str, dict] = {
    "carte": {"purpose": "PAGE_CARTE", "button": "Ouvrir ma carte",
              "text": "📖 Votre carte : ajoutez un plat, changez un prix ou une photo, dites qu'un plat est épuisé.",
              "routes": [("GET", r"/api/v1/products"), ("POST", r"/api/v1/products"),
                         ("PUT", rf"/api/v1/products/{_ID}"), ("POST", rf"/api/v1/products/{_ID}/unavailable-today"),
                         ("POST", rf"/api/v1/products/{_ID}/image")]},
    "horaires": {"purpose": "PAGE_HORAIRES", "button": "Ouvrir mes horaires",
                 "text": "🕒 Vos horaires : la cuisine, les réservations de table, et la fermeture de ce soir.",
                 "routes": [("GET", r"/api/v1/tenants/me/fulfillment"), ("PUT", r"/api/v1/tenants/me/fulfillment"),
                            ("GET", r"/api/v1/appointments/settings"), ("PUT", r"/api/v1/appointments/settings"),
                            ("GET", r"/api/v1/tenants/me/kitchen-closed"), ("PUT", r"/api/v1/tenants/me/kitchen-closed")]},
    "affiches": {"purpose": "PAGE_AFFICHES", "button": "Ouvrir mes affiches",
                 "text": "🖨️ Vos affiches : vitrine, carte de comptoir, plat du jour et QR de table, prêts à imprimer.",
                 "routes": [("GET", r"/api/v1/kit"), ("GET", r"/api/v1/kit/poster"), ("GET", r"/api/v1/kit/poster-options"),
                            ("GET", r"/api/v1/table-qr-codes"), ("POST", r"/api/v1/table-qr-codes"),
                            ("GET", r"/api/v1/table-qr-codes/sheet"), ("GET", rf"/api/v1/table-qr-codes/{_ID}/qr\.svg"),
                            ("GET", r"/api/v1/qr-codes"), ("GET", rf"/api/v1/qr-codes/{_ID}/poster")]},
}
_BY_PURPOSE = {spec["purpose"]: name for name, spec in PAGES.items()}


def scope_of(purpose: str | None) -> str | None:
    """« PAGE_CARTE » → « page:carte » ; tout autre usage (lien de Naomy, lot 118) → None (session normale)."""
    name = _BY_PURPOSE.get(purpose or "")
    return f"page:{name}" if name else None


def allowed(scope: str, method: str, path: str) -> bool:
    name = scope.removeprefix("page:")
    spec = PAGES.get(name)
    if spec is None:
        return False
    path = path.rstrip("/") or "/"
    return any(method.upper() == m and re.fullmatch(pattern, path) for m, pattern in _COMMON + spec["routes"])


def page_url(name: str, token: str, country: str | None = None) -> str:
    from app.core.config import get_settings
    from app.services.prospection import naomy_number_for

    number = naomy_number_for(country)  # lot 134 : bouton « Redemander le lien à Bob » vers le bon numéro AgenC'AI
    return (f"{get_settings().public_base_url.rstrip('/')}/m/?page={name}&connexion={token}"
            + (f"&wa={number}" if number else ""))


async def create(db, tenant, name: str, now: datetime | None = None) -> str | None:
    """Le lien de la page pour le propriétaire de la boutique (à défaut un administrateur), ou None : pas de compte
    possible (double vérification, compte désactivé) ou trop de liens aujourd'hui."""
    import secrets

    from sqlalchemy import case, func, select

    from app.models.magic_login import MagicLogin
    from app.models.user import User
    from app.services.magic_login import _digest

    spec = PAGES[name]
    now = now or datetime.now(timezone.utc)
    user = (await db.execute(select(User).where(User.tenant_id == tenant.id, User.active.is_(True),
                                                User.role.in_(("OWNER", "ADMIN")))
                             .order_by(case((User.role == "OWNER", 0), else_=1), User.created_at))).scalars().first()
    if user is None or getattr(user, "mfa_enabled", False):
        return None
    issued = (await db.execute(select(func.count(MagicLogin.id)).where(
        MagicLogin.tenant_id == tenant.id, MagicLogin.purpose.like(f"{PURPOSE_PREFIX}%"),
        MagicLogin.created_at >= now - timedelta(hours=24)))).scalar_one()
    if issued >= PER_DAY:
        return None
    token = secrets.token_urlsafe(32)
    db.add(MagicLogin(tenant_id=tenant.id, user_id=user.id, token_hash=_digest(token), purpose=spec["purpose"],
                      expires_at=now + VALID, created_at=now))
    return page_url(name, token, getattr(tenant, "country", None))
