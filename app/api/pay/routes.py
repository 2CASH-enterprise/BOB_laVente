"""
Lot 68 — page « Payer ma commande » (bouton du modèle WhatsApp « commande en attente de paiement ») :
…/c/CODE affiche le récapitulatif de LA commande, le moyen de paiement de la boutique et comment envoyer la
preuve. Code impossible à deviner ; ni adresse, ni numéro du client ; jamais indexée par les moteurs.
"""
import re
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.order import Order, OrderStatus

router = APIRouter(tags=["pay"])

CODE_RE = re.compile(r"^[A-Z0-9]{6,16}$")

STYLE = """
:root { color-scheme: light dark; --bg:#F5F8F6; --panel:#FFFFFF; --border:#E3EBE6; --text:#13241A; --muted:#5B6F65;
  --accent:#1B4332; --on:#FFFFFF; --soft:#E3F2EA; --ok:#23794F; --okSoft:#E3F4EB; --warn:#9A5D0A; --warnSoft:#FBF1E1; }
@media (prefers-color-scheme: dark) { :root { --bg:#0D1511; --panel:#15201A; --border:#24342B; --text:#E4EEE8; --muted:#9AAEA2;
  --accent:#52B788; --on:#0D1511; --soft:#1C3327; --ok:#5CC08A; --okSoft:#173226; --warn:#E0A955; --warnSoft:#33291A; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 15px; line-height: 1.5; }
main { max-width: 480px; margin: 0 auto; padding: 24px 16px 40px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 18px; padding: 22px 20px; }
h1 { font-size: 21px; margin: 4px 0 2px; } .muted { color: var(--muted); font-size: 13.5px; }
.shop { font-weight: 800; font-size: 13px; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); }
ul { list-style: none; margin: 16px 0; padding: 0; border-top: 1px solid var(--border); }
li { padding: 10px 0; border-bottom: 1px solid var(--border); overflow-wrap: anywhere; }
.total { display: flex; justify-content: space-between; font-weight: 800; font-size: 18px; margin: 4px 0 18px; }
.btn { display: block; text-align: center; text-decoration: none; padding: 14px 16px; border-radius: 12px; font-weight: 800; margin-top: 10px; }
.btn.primary { background: var(--accent); color: var(--on); } .btn.ghost { border: 1px solid var(--border); color: var(--text); }
.note { background: var(--soft); border-radius: 12px; padding: 12px 14px; font-size: 14px; margin-top: 14px; }
.state { display: inline-block; border-radius: 999px; padding: 4px 12px; font-size: 12.5px; font-weight: 800; }
.state.wait { background: var(--warnSoft); color: var(--warn); } .state.ok { background: var(--okSoft); color: var(--ok); }
footer { text-align: center; margin-top: 18px; }
"""


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    html = (f'<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">'
            f'<title>{escape(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>')
    return HTMLResponse(html, status_code=status_code, headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"})


def _missing() -> HTMLResponse:
    return _page("Commande introuvable", '<div class="card"><h1>Commande introuvable</h1><p class="muted">Ce lien n\'est plus '
                 'valable. Répondez simplement au message WhatsApp de la boutique : elle vous aidera.</p></div>', 404)


@router.get("/c/{code}", response_class=HTMLResponse, include_in_schema=False)
async def pay_page(code: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    from app.models.tenant import Tenant
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services.address_form import uses_tu
    from app.services.order_emails import _money, _ref
    from app.services.receipt_service import get_order_item_lines

    code = (code or "").strip().upper()
    if not CODE_RE.match(code):
        return _missing()
    order = (await db.execute(select(Order).where(Order.pay_code == code))).scalar_one_or_none()
    tenant = await db.get(Tenant, order.tenant_id) if order else None
    if order is None or tenant is None:
        return _missing()
    tu = uses_tu(tenant)
    items = await get_order_item_lines(db, order.id, order.currency)
    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one_or_none()
    digits = re.sub(r"\D", "", (account.display_phone_number or "") if account else "")
    ref = _ref(order)
    status = OrderStatus(order.status)
    if status == OrderStatus.PAID:
        state = '<span class="state ok">Payée</span>'
    elif status == OrderStatus.CANCELLED:
        state = '<span class="state wait">Annulée</span>'
    else:
        state = '<span class="state wait">En attente de paiement</span>'
    parts = [f'<div class="card"><div class="shop">{escape(tenant.name)}</div>',
             f'<h1>{"Ta" if tu else "Votre"} commande {escape(ref)}</h1>{state}',
             "<ul>" + "".join(f"<li>{escape(line)}</li>" for line in items) + "</ul>",
             f'<div class="total"><span>Total</span><span>{escape(_money(order.total_amount, order.currency))}</span></div>']
    if status == OrderStatus.PAID:
        parts.append(f'<div class="note">Paiement bien reçu, merci {"pour ta" if tu else "pour votre"} confiance !</div>')
    elif status == OrderStatus.CANCELLED:
        parts.append('<div class="note">Cette commande a été annulée. ' + ("Écris" if tu else "Écrivez") +
                     " à la boutique sur WhatsApp pour en passer une nouvelle.</div>")
    else:
        link = (tenant.payment_link or "").strip()
        if link.lower().startswith(("https://", "http://")):
            parts.append(f'<a class="btn primary" href="{escape(link)}" rel="noopener">Payer {escape(_money(order.total_amount, order.currency))}</a>')
        elif link:
            parts.append(f'<div class="note"><b>Pour payer :</b> {escape(link)}</div>')
        else:
            parts.append('<div class="note">' + ("Écris" if tu else "Écrivez") + " à la boutique sur WhatsApp : elle "
                         + ("t'indiquera" if tu else "vous indiquera") + " comment payer.</div>")
        parts.append('<div class="note">' + ("Une fois le paiement fait, envoie" if tu else "Une fois le paiement fait, envoyez")
                     + " la capture d'écran sur WhatsApp : la boutique " + ("t'enverra ton" if tu else "vous enverra votre")
                     + " reçu.</div>")
        if digits:
            text = f"Bonjour, voici la preuve de paiement de la commande {ref}."
            parts.append(f'<a class="btn ghost" href="https://wa.me/{digits}?text={escape(quote(text))}" rel="noopener">'
                         "Envoyer la capture sur WhatsApp</a>")
    parts.append("</div><footer class=\"muted\">Commande passée avec Bob, le vendeur WhatsApp de "
                 f"{escape(tenant.name)}</footer>")
    return _page(f"Commande {ref} — {tenant.name}", "".join(parts))
