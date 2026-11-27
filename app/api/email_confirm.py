"""Lot 134 — page publique « Confirmer mon adresse » : l'ouvrir ne confirme rien (antivirus, aperçus), le bouton oui."""
from html import escape

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services import email_confirm

router = APIRouter()

_STYLE = ("<style>body{margin:0;background:#F6FBF8;color:#111F18;font-family:-apple-system,Segoe UI,Roboto,sans-serif}"
          ".c{max-width:440px;margin:60px auto;background:#fff;border-radius:20px;padding:28px 24px;text-align:center;"
          "box-shadow:0 8px 22px rgba(7,94,84,.08)}h1{font-size:22px;margin:0 0 10px;color:#075E54}p{color:#3D5A4F}"
          "button{font:inherit;font-weight:800;border:0;border-radius:999px;background:#25D366;color:#111F18;"
          "padding:14px 26px;min-height:48px;cursor:pointer}</style>")
MESSAGES = {
    "ok": ("Adresse confirmée", "Merci ! Vous recevrez désormais vos rapports à cette adresse, et pourrez vous y connecter."),
    "inconnu": ("Lien expiré ou déjà utilisé", "Demandez un nouveau lien à Naomy sur WhatsApp, ou écrivez à contact@agenc-ai.com."),
    "prise": ("Adresse déjà utilisée", "Cette adresse appartient déjà à un autre compte Bob. Écrivez à contact@agenc-ai.com : nous vous aiderons."),
}


def _page(title: str, inner: str) -> HTMLResponse:
    return HTMLResponse(f'<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" '
                        f'content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">'
                        f'<title>{escape(title)} — Bob</title>{_STYLE}</head><body><div class="c">{inner}</div></body></html>',
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/email/confirmer", include_in_schema=False)
async def confirm_page(jeton: str = Query(default="", max_length=100)) -> HTMLResponse:
    return _page("Confirmer mon adresse", '<h1>Confirmer mon adresse</h1><p>Un toucher suffit.</p>'
                 f'<form method="post" action=""><input type="hidden" name="jeton" value="{escape(jeton)}">'
                 '<button type="submit">Confirmer mon adresse</button></form>')


@router.post("/email/confirmer", include_in_schema=False)
async def confirm_submit(jeton: str = Form(default="", max_length=100), db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    title, text = MESSAGES[await email_confirm.confirm(db, jeton)]
    return _page(title, f"<h1>{escape(title)}</h1><p>{escape(text)}</p>")
