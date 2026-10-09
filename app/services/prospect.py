"""
Lot 43 — fiche prospect et score commercial (concession automobile).

- Bob qualifie progressivement (une ou deux questions à la fois) et enregistre ce qu'il apprend
  avec l'outil update_prospect_profile : type de véhicule et usage, neuf ou occasion, budget,
  comptant ou financement, délai d'achat, véhicule à reprendre. La ville vient de la fiche client.
- La fiche réunit ces informations et celles du dernier rendez-vous (sans jamais les inventer).
- Le score Chaud / Tiède / Froid est calculé par des RÈGLES FIXES (jamais un avis de l'IA), et
  chaque score est expliqué au commercial en une ligne :
    Vendu  = le dernier rendez-vous a abouti à une vente (ce n'est plus un prospect)
    Chaud  = achat sous 3 mois ET (rendez-vous pris OU budget connu)
             (02/10 : en Afrique, un achat dans les 3 mois est un projet concret, pas seulement sous 1 mois)
    Tiède  = rendez-vous pris, OU venu et à relancer, OU achat sous 3 mois, OU besoin et budget connus
    Froid  = sinon (simple renseignement, venu sans intérêt, absent au rendez-vous)
- Un prospect qui devient Chaud SANS rendez-vous déclenche un email « Prospect chaud à rappeler »
  à la concession et à son commercial, une seule fois (réservé en base).
"""
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.models.appointment_request import STATUS_CANCELLED, AppointmentRequest
from app.models.prospect_profile import ProspectProfile
from app.services.business_type import is_dealership

CONDITIONS = {"NEUF": "Neuf", "OCCASION": "Occasion", "INDIFFERENT": "Neuf ou occasion"}
PAYMENTS = {"COMPTANT": "Comptant", "FINANCEMENT": "Financement", "A_DEFINIR": "À définir"}
TIMELINES = {"MOINS_1_MOIS": "achat sous 1 mois", "UN_A_3_MOIS": "achat dans 1 à 3 mois",
             "PLUS_3_MOIS": "achat dans plus de 3 mois", "NE_SAIT_PAS": "pas encore de date"}
SOON = ("MOINS_1_MOIS", "UN_A_3_MOIS")  # « Chaud » possible : achat dans les 3 mois
SCORES = {"VENDU": "Vendu", "CHAUD": "Chaud", "TIEDE": "Tiède", "FROID": "Froid"}
OUTCOME_REASONS = {"FOLLOW_UP": "venu, à relancer", "NOT_INTERESTED": "venu, pas intéressé", "NO_SHOW": "absent au rendez-vous"}
TEXT_LIMITS = {"need": 300, "budget": 100, "trade_in": 300}


def _clean(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:limit] or None


async def load(db, tenant_id, customer_id) -> ProspectProfile | None:
    return (await db.execute(select(ProspectProfile).where(
        ProspectProfile.tenant_id == tenant_id, ProspectProfile.customer_id == customer_id,
    ))).scalar_one_or_none()


async def update_profile(db, tenant_id, customer_id, values: dict) -> tuple[ProspectProfile, list[str]]:
    """Enregistre ce que le client a dit (valeurs vérifiées) ; renvoie la fiche et les champs refusés."""
    profile = await load(db, tenant_id, customer_id)
    if profile is None:
        profile = ProspectProfile(tenant_id=tenant_id, customer_id=customer_id)
        db.add(profile)
    rejected = []
    for key, limit in TEXT_LIMITS.items():
        if values.get(key) is not None:
            cleaned = _clean(values.get(key), limit)
            if cleaned:
                setattr(profile, key, cleaned)
            else:
                rejected.append(key)
    for key, allowed in (("condition", CONDITIONS), ("payment", PAYMENTS), ("timeline", TIMELINES)):
        if values.get(key) is not None:
            if values[key] in allowed:
                setattr(profile, key, values[key])
            else:
                rejected.append(key)
    await db.flush()
    return profile, rejected


async def _latest_appointment(db, tenant_id, customer_id) -> AppointmentRequest | None:
    return (await db.execute(select(AppointmentRequest).where(
        AppointmentRequest.tenant_id == tenant_id, AppointmentRequest.customer_id == customer_id,
        AppointmentRequest.status != STATUS_CANCELLED,
    ).order_by(AppointmentRequest.created_at.desc()).limit(1))).scalar_one_or_none()


def _has_upcoming(appointment: AppointmentRequest | None, now: datetime) -> bool:
    """Rendez-vous demandé, ou confirmé et pas encore passé (un rendez-vous passé n'est plus un projet)."""
    if appointment is None:
        return False
    if appointment.scheduled_at is None:
        return True
    scheduled = appointment.scheduled_at if appointment.scheduled_at.tzinfo else appointment.scheduled_at.replace(tzinfo=timezone.utc)
    return scheduled >= now


def score(fiche: dict) -> tuple[str, list[str]]:
    """(CHAUD | TIEDE | FROID, raisons lisibles). Règles fixes, voir l'en-tête du module."""
    outcome = fiche.get("outcome")
    if outcome == "SOLD":
        return "VENDU", ["venu, vendu"]
    reasons = []
    if outcome in OUTCOME_REASONS:
        reasons.append(OUTCOME_REASONS[outcome])
    if fiche["has_appointment"]:
        reasons.append("rendez-vous pris")
    if fiche["timeline"]:
        reasons.append(TIMELINES[fiche["timeline"]])
    if fiche["budget"]:
        reasons.append("budget connu")
    urgent = fiche["timeline"] in SOON  # achat sous 3 mois
    if urgent and (fiche["has_appointment"] or fiche["budget"]):
        return "CHAUD", reasons
    if fiche["has_appointment"] or outcome == "FOLLOW_UP" or fiche["timeline"] in SOON \
            or (fiche["need"] and fiche["budget"]):
        return "TIEDE", reasons
    return "FROID", reasons or ["simple renseignement pour l'instant"]


async def build_fiche(db, tenant, customer, now: datetime | None = None) -> dict | None:
    """Fiche du prospect (concession, et courtier depuis le lot 54) ; None si Bob n'a encore rien appris."""
    from app.services.business_type import is_insurance

    if tenant is not None and customer is not None and is_insurance(tenant):
        from app.services import insurance_prospect

        return await insurance_prospect.build_fiche(db, tenant, customer, now)
    if tenant is None or customer is None or not is_dealership(tenant):
        return None
    now = now or datetime.now(timezone.utc)
    profile = await load(db, tenant.id, customer.id)
    appointment = await _latest_appointment(db, tenant.id, customer.id)
    if profile is None and appointment is None:
        return None
    from app.services.handoff_service import customer_display_name

    payment = profile.payment if profile else None
    if payment is None and appointment is not None and appointment.financing_interest is not None:
        payment = "FINANCEMENT" if appointment.financing_interest else "COMPTANT"
    fiche = {
        "prospect": " ".join(p for p in (customer.first_name, customer.last_name) if p) or customer_display_name(customer),
        "phone": f"+{customer.whatsapp_number}" if customer.whatsapp_number and customer.whatsapp_number.isdigit() else None,
        "city": customer.city,
        "need": (profile.need if profile else None) or (appointment.need if appointment else None),
        "vehicle": appointment.vehicle_label if appointment else None,
        "condition": profile.condition if profile else None,
        "budget": (profile.budget if profile else None) or (appointment.budget if appointment else None),
        "payment": payment,
        "trade_in": (profile.trade_in if profile else None) or (appointment.trade_in if appointment else None),
        "timeline": profile.timeline if profile else None,
        "has_appointment": _has_upcoming(appointment, now),
        "outcome": appointment.outcome if appointment is not None else None,  # lot 36 : issue du dernier rendez-vous
    }
    fiche["score"], fiche["reasons"] = score(fiche)
    fiche["score_label"] = SCORES[fiche["score"]]
    fiche["lines"] = fiche_lines(fiche)
    return fiche


def fiche_lines(fiche: dict) -> list[str]:
    """« Libellé : valeur » (texte des emails ; tableau dans leur version HTML, lot 40)."""
    rows = [
        ("Prospect", fiche["prospect"]),
        ("Téléphone", fiche["phone"]),
        ("Ville", fiche["city"]),
        ("Véhicule recherché", fiche["need"]),
        ("Véhicule visé", fiche["vehicle"]),
        ("Neuf ou occasion", CONDITIONS.get(fiche["condition"])),
        ("Budget", fiche["budget"]),
        ("Paiement", PAYMENTS.get(fiche["payment"])),
        ("Reprise", fiche["trade_in"]),
        ("Projet", TIMELINES.get(fiche["timeline"])),
        ("Score commercial", f"{SCORES[fiche['score']]} ({', '.join(fiche['reasons'])})"),
    ]
    return [f"{label} : {' '.join(str(value).split())}" for label, value in rows if value]


def fiche_block(fiche: dict | None) -> str:
    """Bloc à insérer dans un email (vide sans fiche)."""
    if not fiche:
        return ""
    return "Fiche prospect\n\n" + "\n".join(fiche["lines"]) + "\n\n"


async def hot_alert_emails(db, tenant, customer, conversation, now: datetime | None = None) -> list[dict]:
    """Prospect devenu Chaud sans rendez-vous : email à la concession et à son commercial, une seule fois."""
    if not is_dealership(tenant):
        return []  # lot 54 : chez le courtier, le score part avec l'email de la demande de cotation
    fiche = await build_fiche(db, tenant, customer, now)
    if not fiche or fiche["score"] != "CHAUD" or fiche["has_appointment"] or fiche["outcome"] is not None:
        return []
    profile = await load(db, tenant.id, customer.id)
    if profile is None or profile.hot_alert_sent_at is not None:
        return []
    claimed = await db.execute(
        update(ProspectProfile).where(ProspectProfile.id == profile.id, ProspectProfile.hot_alert_sent_at.is_(None))
        .values(hot_alert_sent_at=now or datetime.now(timezone.utc)).execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return []
    profile.hot_alert_sent_at = now or datetime.now(timezone.utc)
    from app.services.handoff_service import alert_emails, commercial_for_customer, conversation_link

    subject = f"Prospect chaud à rappeler : {fiche['prospect']}"
    body = ("Bonjour,\n\nBob a qualifié un prospect chaud qui n'a pas encore pris rendez-vous : "
            "rappelez-le rapidement pour lui proposer un essai ou une visite.\n\n"
            + fiche_block(fiche)
            + f"Ouvrir la conversation : {conversation_link(conversation)}")
    return alert_emails(tenant.email, await commercial_for_customer(db, customer), subject, body)
