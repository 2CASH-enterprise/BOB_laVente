"""
Page publique de désinscription (lien présent dans chaque email de campagne).

- GET  : affiche une confirmation avec un bouton. N'ENREGISTRE RIEN — les antivirus de
         messagerie ouvrent automatiquement tous les liens des emails pour les analyser ;
         si l'ouverture suffisait, des clients seraient désinscrits à leur insu.
- POST : enregistre le retrait. Sert à la fois au bouton de la page et au bouton
         « Se désinscrire » natif de Gmail/Outlook (RFC 8058, List-Unsubscribe-Post).

La page n'affiche aucune donnée personnelle : uniquement le nom du commerce.
"""
from html import escape

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.customer import Customer
from app.models.tenant import Tenant
from app.services.consent_service import WITHDRAWN_VIA_EMAIL_LINK, withdraw_marketing_consent
from app.services.unsubscribe_service import read_unsubscribe_token

router = APIRouter(tags=["unsubscribe"])

_PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="referrer" content="no-referrer">
<title>{title}</title>
<style>
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f4f5f7; color: #1f2933; display: flex; min-height: 100vh;
         align-items: center; justify-content: center; padding: 16px; box-sizing: border-box; }}
  .card {{ background: #fff; max-width: 420px; width: 100%; padding: 32px 28px; border-radius: 16px;
          box-shadow: 0 4px 24px rgba(0,0,0,.06); text-align: center; }}
  h1 {{ font-size: 20px; margin: 0 0 12px; }}
  p {{ font-size: 15px; line-height: 1.5; color: #52606d; margin: 0 0 20px; }}
  button {{ background: #1f2933; color: #fff; border: 0; border-radius: 10px; padding: 12px 20px;
           font-size: 15px; cursor: pointer; width: 100%; }}
  button:hover {{ background: #323f4b; }}
  .muted {{ font-size: 12px; color: #9aa5b1; margin: 20px 0 0; }}
</style>
</head>
<body><div class="card">{content}</div></body>
</html>"""


def _render(title: str, content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=escape(title), content=content), status_code=status_code)


def _invalid_link_page() -> HTMLResponse:
    return _render(
        "Lien invalide",
        "<h1>Lien invalide</h1><p>Ce lien de désinscription n'est pas valide. "
        "Vérifiez que vous avez copié l'adresse en entier, ou utilisez le lien présent dans l'email reçu.</p>",
        status_code=404,
    )


def _done_page(shop: str) -> HTMLResponse:
    return _render(
        "Désinscription confirmée",
        f"<h1>C'est fait ✅</h1><p>Vous ne recevrez plus d'offres de <strong>{shop}</strong>.</p>"
        "<p class=\"muted\">Vous pourrez toujours échanger avec ce commerce sur WhatsApp si vous le souhaitez.</p>",
    )


async def _resolve(db: AsyncSession, token: str) -> tuple[Customer, Tenant] | None:
    customer_id = read_unsubscribe_token(token)
    if customer_id is None:
        return None
    customer = await db.get(Customer, customer_id)
    if customer is None:
        return None
    tenant = await db.get(Tenant, customer.tenant_id)
    if tenant is None:
        return None
    return customer, tenant


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_page(token: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    resolved = await _resolve(db, token)
    if resolved is None:
        return _invalid_link_page()
    customer, tenant = resolved
    shop = escape(tenant.name)

    if not customer.marketing_consent:
        return _done_page(shop)

    # Formulaire sans « action » : il renvoie vers l'URL courante, quel que soit le préfixe
    # du reverse proxy (/bob/…).
    return _render(
        "Se désinscrire",
        f"<h1>Se désinscrire</h1><p>Vous ne souhaitez plus recevoir les offres de <strong>{shop}</strong> ?</p>"
        "<form method=\"post\"><button type=\"submit\">Confirmer ma désinscription</button></form>"
        "<p class=\"muted\">Aucune autre action n'est nécessaire.</p>",
    )


@router.post("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_confirm(token: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """Idempotent : un deuxième clic (ou le bouton Gmail après le bouton de la page) ne change rien."""
    resolved = await _resolve(db, token)
    if resolved is None:
        return _invalid_link_page()
    customer, tenant = resolved

    if withdraw_marketing_consent(customer, source=WITHDRAWN_VIA_EMAIL_LINK):
        await db.commit()
    return _done_page(escape(tenant.name))
