"""
Lot 58 — exports du courtier (règlement CIMA 01-24, art. 4, 5, 14 et 15 : le cabinet garde l'accès à ses données,
piste d'audit de l'origine au dénouement, communication aux autorités et contrôles sur place).

- Registre des contrats et demandes de cotation en CSV (« ; », UTF-8 avec BOM pour Excel), heures du pays.
- « Dossier du client » : une page autonome, imprimable (Enregistrer en PDF), qui rassemble tout ce que Bob et le
  cabinet savent d'un client : identité, accords donnés, conversations complètes horodatées, demandes de cotation
  et leurs étapes, rendez-vous, contrats, réclamations. Tout le texte est échappé : un message de client ne peut
  rien injecter dans la page.

La prime n'apparaît que pour un administrateur du cabinet (choix du lot 55).
"""
import csv
import io
from datetime import datetime, timezone
from html import escape

from sqlalchemy import select

from app.services import insurance


def _zone(tenant):
    from app.services.local_time import tenant_zone

    return tenant_zone(tenant)


def _when(tenant, moment) -> str:
    if moment is None:
        return ""
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(_zone(tenant)).strftime("%d/%m/%Y %H:%M")


def _day(value) -> str:
    return value.strftime("%d/%m/%Y") if value else ""


def _csv(rows: list[list]) -> bytes:
    out = io.StringIO()
    csv.writer(out, delimiter=";").writerows(rows)
    return out.getvalue().encode("utf-8-sig")


# --- Registre des contrats ------------------------------------------------------------------------

async def contracts_csv(db, tenant, can_see_premium: bool) -> bytes:
    from app.models.customer import Customer
    from app.models.insurance_contract import InsuranceContract
    from app.services import insurance_contracts as ic
    from app.services.handoff_service import customer_display_name

    rows = (await db.execute(select(InsuranceContract, Customer).join(Customer, Customer.id == InsuranceContract.customer_id)
                             .where(InsuranceContract.tenant_id == tenant.id, Customer.tenant_id == tenant.id)
                             .order_by(InsuranceContract.expires_on, InsuranceContract.created_at))).all()
    header = ["Client", "Téléphone", "Email", "Branche", "Assureur", "N° de contrat", "Date d'effet", "Échéance", "Durée",
              "Statut", "Alerte au cabinet", "Rappel au client", "Canal du rappel", "Prise en charge de l'échéance",
              "Renouvelle le contrat", "Note", "Créé le"]
    if can_see_premium:
        header[7:7] = ["Prime", "Devise"]
    lines = [header]
    for c, customer in rows:
        line = [customer_display_name(customer), f"+{customer.whatsapp_number}", customer.email or "",
                insurance.branch_label(c.branch), c.insurer or "", c.policy_number or "", _day(c.effective_on),
                _day(c.expires_on), insurance.TERMS.get(c.term or "", ""), ic.STATUS_LABELS.get(c.status, c.status),
                _when(tenant, c.broker_alerted_at), _when(tenant, c.client_reminded_at),
                ic.CHANNEL_LABELS.get(c.client_reminder_channel or "", ""), _when(tenant, c.renewal_handled_at),
                "oui" if c.renewed_from_id else "", c.note or "", _when(tenant, c.created_at)]
        if can_see_premium:
            line[7:7] = [f"{c.premium:.2f}".replace(".", ",") if c.premium is not None else "", c.currency or ""]
        lines.append(line)
    return _csv(lines)


# --- Demandes de cotation -------------------------------------------------------------------------

async def quotes_csv(db, tenant) -> bytes:
    from app.models.customer import Customer
    from app.models.quote_request import QuoteRequest
    from app.services.handoff_service import customer_display_name

    rows = (await db.execute(select(QuoteRequest, Customer).join(Customer, Customer.id == QuoteRequest.customer_id)
                             .where(QuoteRequest.tenant_id == tenant.id, Customer.tenant_id == tenant.id)
                             .order_by(QuoteRequest.created_at))).all()
    lines = [["Client", "Téléphone", "Branche", "Type de client", "Statut", "Informations", "Pays du risque",
              "Alerte de domiciliation", "Créée le", "Accord du client", "Transmise le", "Prise en charge le",
              "Proposition envoyée le", "Terminée le", "Motif de perte"]]
    for r, customer in rows:
        details = r.details or {}
        info = " | ".join(line for line in insurance.request_lines(r)[2:] if not line.startswith("Pays du risque"))
        lines.append([customer_display_name(customer), f"+{customer.whatsapp_number}", insurance.branch_label(r.branch),
                      insurance.CLIENT_TYPES.get(r.client_type, ""), insurance.STATUS_LABELS.get(r.status, r.status), info,
                      insurance.country_name(details["risk_country"]) if details.get("risk_country") else "",
                      insurance.domiciliation_warning(tenant, r) or "", _when(tenant, r.created_at),
                      _when(tenant, r.consent_at), _when(tenant, r.submitted_at), _when(tenant, r.handled_at),
                      _when(tenant, r.proposal_sent_at), _when(tenant, r.closed_at), r.lost_reason or ""])
    return _csv(lines)


# --- Dossier du client ----------------------------------------------------------------------------

SENDERS = {"CUSTOMER": "Client", "AI": "Bob", "HUMAN": "Conseiller", "SYSTEM": "Bob (message automatique)"}

_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{title}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; background: #f4f5f7; color: #1f2933; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  main {{ max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }}
  header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; flex-wrap: wrap; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }} h2 {{ font-size: 16px; margin: 28px 0 8px; border-bottom: 1px solid #d9dee5; padding-bottom: 4px; }}
  h3 {{ font-size: 14px; margin: 16px 0 6px; }}
  .muted {{ color: #616e7c; font-size: 12px; }}
  .card {{ background: #fff; border: 1px solid #d9dee5; border-radius: 10px; padding: 12px 14px; margin: 8px 0; break-inside: avoid; }}
  table {{ border-collapse: collapse; width: 100%; }} td {{ padding: 3px 8px 3px 0; vertical-align: top; }}
  td:first-child {{ color: #616e7c; white-space: nowrap; width: 1%; }}
  .msg {{ display: grid; grid-template-columns: 120px 1fr; gap: 8px; padding: 6px 0; border-top: 1px solid #eef0f3; }}
  .msg:first-child {{ border-top: 0; }} .who {{ font-weight: 600; font-size: 12px; }} .text {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
  button {{ background: #1f3d2f; color: #fff; border: 0; border-radius: 8px; padding: 10px 16px; font: inherit; font-weight: 600; cursor: pointer; }}
  @media print {{ body {{ background: #fff; }} button {{ display: none; }} main {{ padding: 0; }} .card {{ border-color: #bbb; }} }}
  @media (max-width: 600px) {{ .msg {{ grid-template-columns: 1fr; gap: 0; }} }}
</style></head>
<body><main>
{body}
</main></body></html>"""


def _table(rows: list[tuple]) -> str:
    cells = "".join(f"<tr><td>{escape(label)}</td><td>{escape(str(value))}</td></tr>" for label, value in rows if value not in (None, ""))
    return f"<table>{cells}</table>" if cells else '<p class="muted">—</p>'


async def dossier_html(db, tenant, customer, user_label: str, can_see_premium: bool, now: datetime | None = None) -> str:
    from app.models.appointment_request import AppointmentRequest
    from app.models.conversation import Conversation, Message
    from app.models.insurance_complaint import InsuranceComplaint
    from app.models.insurance_contract import InsuranceContract
    from app.models.quote_request import QuoteRequest
    from app.services import insurance_complaints as icp
    from app.services import insurance_contracts as ict
    from app.services.handoff_service import customer_display_name

    now = now or datetime.now(timezone.utc)
    who = customer_display_name(customer)
    parts = [
        "<header><div>"
        f"<h1>Dossier du client — {escape(who)}</h1>"
        f"<p class=\"muted\">{escape(tenant.name)}"
        + (f" · {escape(insurance.STRUCTURES.get(tenant.insurance_structure or '', ''))}" if tenant.insurance_structure else "")
        + (f" · agrément {escape(tenant.insurance_license)}" if tenant.insurance_license else "")
        + f"<br>Édité le {escape(_when(tenant, now))} (heure de {escape(tenant.country)}) par {escape(user_label)}. "
        "Document interne au cabinet : conservez-le en lieu sûr.</p></div>"
        '<button type="button" onclick="window.print()">Imprimer / Enregistrer en PDF</button></header>',
        "<h2>Identité et accords</h2>",
        '<div class="card">' + _table([
            ("Nom", " ".join(p for p in (customer.first_name, customer.last_name) if p)),
            ("Téléphone WhatsApp", f"+{customer.whatsapp_number}"),
            ("Email", customer.email),
            ("Ville", customer.city),
            ("Premier contact", _when(tenant, customer.created_at)),
            ("Accord pour recevoir les offres", ("oui, le " + _when(tenant, customer.marketing_consent_given_at))
             if customer.marketing_consent and customer.marketing_consent_given_at else "non"),
            ("Retrait de l'accord", _when(tenant, customer.marketing_consent_withdrawn_at)),
            ("Information sur ses données envoyée le", _when(tenant, customer.privacy_notice_at)),
            ("Note du cabinet", customer.notes),
        ]) + "</div>",
    ]

    quotes = (await db.execute(select(QuoteRequest).where(QuoteRequest.tenant_id == tenant.id,
                                                          QuoteRequest.customer_id == customer.id)
                               .order_by(QuoteRequest.created_at))).scalars().all()
    parts.append(f"<h2>Demandes de cotation ({len(quotes)})</h2>")
    for r in quotes:
        warning = insurance.domiciliation_warning(tenant, r)
        parts.append('<div class="card">' + f"<h3>{escape(insurance.branch_label(r.branch))} — "
                     f"{escape(insurance.STATUS_LABELS.get(r.status, r.status))}</h3>"
                     + _table([(line.split(" : ", 1)[0], line.split(" : ", 1)[-1]) for line in insurance.request_lines(r)[1:]])
                     + (f"<p><strong>⚠️ {escape(warning)}</strong></p>" if warning else "")
                     + "".join(f'<p class="muted">{escape(line)}</p>' for line in insurance.trace_lines(r))
                     + (f'<p class="muted">Créée le {escape(_when(tenant, r.created_at))}</p>') + "</div>")
    if not quotes:
        parts.append('<p class="muted">Aucune.</p>')

    appointments = (await db.execute(select(AppointmentRequest).where(AppointmentRequest.tenant_id == tenant.id,
                                                                      AppointmentRequest.customer_id == customer.id)
                                     .order_by(AppointmentRequest.created_at))).scalars().all()
    parts.append(f"<h2>Rendez-vous et appels ({len(appointments)})</h2>")
    for a in appointments:
        parts.append('<div class="card">' + _table([
            ("Type", {"APPEL": "Appel", "CABINET": "Rendez-vous au cabinet"}.get(a.kind, a.kind)),
            ("Demandé le", _when(tenant, a.created_at)),
            ("Disponibilités données", a.availability),
            ("Prévu le", _when(tenant, a.scheduled_at)),
            ("Statut", a.status),
            ("Issue", {"SOLD": "Contrat souscrit", "FOLLOW_UP": "À relancer", "NOT_INTERESTED": "Pas intéressé",
                       "NO_SHOW": "Absent / injoignable"}.get(a.outcome or "", a.outcome)),
            ("Issue notée le", _when(tenant, a.outcome_at)),
        ]) + "</div>")
    if not appointments:
        parts.append('<p class="muted">Aucun.</p>')

    contracts = (await db.execute(select(InsuranceContract).where(InsuranceContract.tenant_id == tenant.id,
                                                                  InsuranceContract.customer_id == customer.id)
                                  .order_by(InsuranceContract.expires_on))).scalars().all()
    parts.append(f"<h2>Contrats ({len(contracts)})</h2>")
    for c in contracts:
        parts.append('<div class="card">' + _table([
            ("Branche", insurance.branch_label(c.branch)), ("Assureur", c.insurer), ("N° de contrat", c.policy_number),
            ("Statut", ict.STATUS_LABELS.get(c.status, c.status)), ("Date d'effet", _day(c.effective_on)),
            ("Échéance", _day(c.expires_on)), ("Durée", insurance.TERMS.get(c.term or "", "")),
            ("Prime", f"{c.premium:,.0f} {c.currency or ''}".replace(",", " ").strip() if can_see_premium and c.premium is not None else ""),
            ("Rappel d'échéance au client", (_when(tenant, c.client_reminded_at) + " — "
                                             + ict.CHANNEL_LABELS.get(c.client_reminder_channel or "", ""))
             if c.client_reminded_at else ""),
            ("Échéance prise en charge le", _when(tenant, c.renewal_handled_at)),
        ]) + "</div>")
    if not contracts:
        parts.append('<p class="muted">Aucun.</p>')

    complaints = (await db.execute(select(InsuranceComplaint).where(InsuranceComplaint.tenant_id == tenant.id,
                                                                    InsuranceComplaint.customer_id == customer.id)
                                   .order_by(InsuranceComplaint.received_at))).scalars().all()
    parts.append(f"<h2>Réclamations et sinistres ({len(complaints)})</h2>")
    for c in complaints:
        parts.append('<div class="card">' + f"<h3>{escape(c.reference)} — {escape(icp.KINDS.get(c.kind, c.kind))} — "
                     f"{escape(icp.STATUSES.get(c.status, c.status))}</h3>" + _table([
                         ("Reçue le", _when(tenant, c.received_at)), ("Canal", icp.CHANNELS.get(c.channel, c.channel)),
                         ("Objet", c.subject), ("Accusé de réception", _when(tenant, c.acknowledged_at)),
                         ("Date limite de réponse", _day(c.due_on)), ("Prise en charge le", _when(tenant, c.started_at)),
                         ("Traitée le", _when(tenant, c.resolved_at)), ("Réponse apportée", c.resolution),
                     ]) + "</div>")
    if not complaints:
        parts.append('<p class="muted">Aucune.</p>')

    conversations = (await db.execute(select(Conversation).where(Conversation.tenant_id == tenant.id,
                                                                 Conversation.customer_id == customer.id)
                                      .order_by(Conversation.created_at))).scalars().all()
    parts.append(f"<h2>Conversations ({len(conversations)})</h2>")
    for conv in conversations:
        messages = (await db.execute(select(Message).where(Message.tenant_id == tenant.id, Message.conversation_id == conv.id)
                                     .order_by(Message.created_at))).scalars().all()
        rows = "".join(
            f'<div class="msg"><div><div class="who">{escape(SENDERS.get(str(m.sender.value if hasattr(m.sender, "value") else m.sender), "—"))}</div>'
            f'<div class="muted">{escape(_when(tenant, m.created_at))}</div></div><div class="text">{escape(m.content or "")}</div></div>'
            for m in messages)
        parts.append(f'<div class="card"><h3>Commencée le {escape(_when(tenant, conv.created_at))} — {len(messages)} messages</h3>'
                     + (rows or '<p class="muted">Aucun message.</p>') + "</div>")
    if not conversations:
        parts.append('<p class="muted">Aucune.</p>')
    return _PAGE.format(title=escape(f"Dossier — {who}"), body="\n".join(parts))
