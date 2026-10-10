"""
Lot 60 — le rapport mensuel envoyé aux administrateurs (propriétaire et admins), choix du 10/10 :
- 7 jours avant le renouvellement de l'abonnement, il REMPLACE l'email d'échéance du lot 51 (même moment,
  un seul email : le rapport, puis le rappel « votre abonnement se termine le … ») ;
- sans échéance proche (plan gratuit, abonnement long), il part en début de mois (du 1er au 3, après 8 h,
  heure du pays), au plus un tous les 20 jours. Jamais pour une démo ni une boutique suspendue.
- Période : depuis la fin du rapport précédent (entre 7 et 62 jours), sinon les 30 derniers jours.

Ce que Bob MESURE (conversations, réponses, heures, demandes, sources) est séparé de ce que le commerçant
DÉCLARE (paiements reçus, ventes, rendez-vous honorés, contrats) : ces chiffres portent « déclaré par vous »
et le bloc « À valider » liste ce qui manque. Le titre ne repose que sur ce que Bob mesure.

Le conseil vient de règles fixes, jamais de l'IA. Aucun montant n'est estimé : le temps libéré est compté
en heures (2 minutes par réponse de Bob), et la présence 24 h / 24 en personnes (173 heures par mois).
"""
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.services.local_time import _MONTHS, as_utc, tenant_zone

logger = logging.getLogger(__name__)

OFFICE_START, OFFICE_END = 8, 19      # heures de bureau, heure du pays (le week-end est entièrement hors heures)
MINUTES_PER_REPLY = 2                 # temps qu'un humain met pour répondre à un message
HOURS_PER_PERSON = 173                # heures de travail d'une personne à temps plein, par mois
DEFAULT_DAYS = 30
MIN_DAYS, MAX_DAYS = 7, 62
REPLY_DELAY_MAX = timedelta(minutes=10)  # au-delà, ce n'est plus une réponse « immédiate » (panne, pause…)
MONTHLY_GAP = timedelta(days=20)      # deux rapports de début de mois : au moins 20 jours d'écart
RENEWAL_WINDOW_DAYS = 14              # échéance dans 14 jours ou moins : c'est le rapport de renouvellement qui part
REPORT_ROLES = ("OWNER", "ADMIN")
INTENTS_SHOWN, ROWS_SHOWN = 5, 5
SKIPPED_INTENTS = ("SALUTATION", "AUTRE")
NBSP, NNBSP = " ", " "


def is_off_hours(moment: datetime, zone) -> bool:
    local = as_utc(moment).astimezone(zone)
    return local.weekday() >= 5 or local.hour < OFFICE_START or local.hour >= OFFICE_END


def number(value) -> str:
    return f"{int(round(value)):,}".replace(",", NNBSP)


def money(value, currency) -> str:
    return f"{number(value)}{NBSP}{currency or ''}".strip()


def short_date(value: date, with_year: bool = True) -> str:
    day = "1er" if value.day == 1 else str(value.day)
    return f"{day} {_MONTHS[value.month - 1]}" + (f" {value.year}" if with_year else "")


def period_label(start: datetime, end: datetime, zone) -> str:
    first = as_utc(start).astimezone(zone).date()
    last = (as_utc(end) - timedelta(seconds=1)).astimezone(zone).date()
    return f"{short_date(first, first.year != last.year)} au {short_date(last)}"


def duration(minutes: int) -> str:
    hours, rest = divmod(int(minutes), 60)
    return f"{hours} h {rest:02d}" if hours else f"{rest} min"


def delta(current: float, previous: float | None) -> str:
    """« +18 % », « −2 » (petits nombres) ; rien sans période précédente comparable."""
    if previous is None or (not current and not previous):
        return ""
    if previous >= 10:
        pct = round((current - previous) / previous * 100)
        return f"+{pct} %" if pct >= 0 else f"−{abs(pct)} %"
    diff = int(round(current - previous))
    return f"+{diff}" if diff >= 0 else f"−{abs(diff)}"


def local_midnight(day: date, zone) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)


def days_window(tenant, now: datetime, days: int, include_today: bool = False) -> tuple[datetime, datetime]:
    """Des journées entières, heure du pays : jusqu'à minuit ce matin (ou ce soir, aujourd'hui compris)."""
    zone = tenant_zone(tenant)
    last = as_utc(now).astimezone(zone).date() + timedelta(days=1 if include_today else 0)
    return local_midnight(last - timedelta(days=days), zone), local_midnight(last, zone)


def report_window(tenant, now: datetime) -> tuple[datetime, datetime]:
    """Depuis la fin du rapport précédent (7 à 62 jours), sinon 30 jours ; journées entières jusqu'à hier soir."""
    days = DEFAULT_DAYS
    start, end = days_window(tenant, now, days)
    last = getattr(tenant, "report_period_end", None)
    if last is not None:
        gap = (end - as_utc(last)).total_seconds() / 86400
        if MIN_DAYS <= gap <= MAX_DAYS:
            start, end = days_window(tenant, now, max(MIN_DAYS, round(gap)))
    return start, end


# --- Données ----------------------------------------------------------------------------------------------

async def build(db, tenant, start: datetime, end: datetime, renewal: bool = False) -> dict:
    """Toutes les parties du rapport pour [start, end) ; le prénom du destinataire est ajouté à l'envoi."""
    from app.core.config import get_settings
    from app.models.conversation import Conversation, Message, MessageSender
    from app.models.customer import Customer
    from app.services import bob_pause
    from app.services.business_type import is_appointment_sector, is_insurance, normalize
    from app.services.home_service import home_summary

    zone = tenant_zone(tenant)
    start, end = as_utc(start), as_utc(end)
    days = max(1, round((end - start).total_seconds() / 86400))
    prev_start = start - (end - start)
    comparable = tenant.created_at is None or as_utc(tenant.created_at) <= prev_start
    sector = normalize(tenant.business_type)
    rdv, insurance = is_appointment_sector(tenant), is_insurance(tenant)
    place = "cabinet" if insurance else "concession" if rdv else "boutique"
    base = get_settings().public_base_url.rstrip("/") + "/dashboard/"

    home = await home_summary(db, tenant.id, days=days, now=end)

    # --- Messages de la période : conversations, réponses de Bob, heures, délai de réponse ---------------
    everything = (await db.execute(select(Message).where(
        Message.tenant_id == tenant.id, Message.created_at >= prev_start, Message.created_at < end,
    ).order_by(Message.created_at))).scalars().all()
    messages = [m for m in everything if as_utc(m.created_at) >= start]
    conv_prev = len({m.conversation_id for m in everything
                     if m.sender == MessageSender.CUSTOMER and as_utc(m.created_at) < start}) if comparable else None
    owner_of = dict((await db.execute(select(Conversation.id, Conversation.customer_id).where(
        Conversation.tenant_id == tenant.id))).all())
    customer_msgs = [m for m in messages if m.sender == MessageSender.CUSTOMER]
    ai_msgs = [m for m in messages if m.sender == MessageSender.AI]
    conversations = {m.conversation_id for m in customer_msgs}
    off_replies = sum(1 for m in ai_msgs if is_off_hours(m.created_at, zone))
    off_customers = {owner_of.get(m.conversation_id) for m in customer_msgs if is_off_hours(m.created_at, zone)} - {None}
    hours = [0] * 24
    per_day: dict = {}
    for m in customer_msgs:
        local = as_utc(m.created_at).astimezone(zone)
        hours[local.hour] += 1
        per_day.setdefault(local.date(), set()).add(m.conversation_id)
    delays = []
    by_conversation: dict = {}
    for m in messages:
        by_conversation.setdefault(m.conversation_id, []).append(m)
    for items in by_conversation.values():
        for current, following in zip(items, items[1:]):
            if current.sender == MessageSender.CUSTOMER and following.sender == MessageSender.AI:
                gap = as_utc(following.created_at) - as_utc(current.created_at)
                if timedelta(0) <= gap <= REPLY_DELAY_MAX:
                    delays.append(gap.total_seconds())

    # --- Nouveaux clients (ceux importés d'un fichier ne sont pas des clients venus par Bob) ----------------
    created = (await db.execute(select(Customer.created_at, Customer.acquisition_source).where(
        Customer.tenant_id == tenant.id, Customer.created_at >= prev_start, Customer.created_at < end))).all()
    new_now = [at for at, source in created if source != "IMPORT" and as_utc(at) >= start]
    new_prev = sum(1 for at, source in created if source != "IMPORT" and as_utc(at) < start)
    new_off = sum(1 for at in new_now if is_off_hours(at, zone))

    # --- Tuiles, parcours, résultats hors heures (selon le secteur) ----------------------------------------
    def tile(label, now_value, prev_value, declared=False):
        return (label, number(now_value), delta(now_value, prev_value if comparable else None), declared)

    def split(moments):
        moments = [as_utc(m) for m in moments if m is not None]
        return sum(1 for m in moments if start <= m < end), sum(1 for m in moments if prev_start <= m < start)

    tiles = [tile("Conversations", len(conversations), conv_prev), tile("Nouveaux clients", len(new_now), new_prev)]
    funnel = [(f["label"], f["value"], "") for f in home["funnel"]]
    obtained_label = "Ventes payées"
    if insurance:
        from app.models.quote_request import QuoteRequest

        from app.models.appointment_request import OUTCOME_SOLD, AppointmentRequest
        from app.services.insurance import STATUS_WON

        submitted = (await db.execute(select(QuoteRequest.submitted_at).where(
            QuoteRequest.tenant_id == tenant.id, QuoteRequest.submitted_at >= prev_start, QuoteRequest.submitted_at < end))).scalars().all()
        off_result = (sum(1 for at in submitted if as_utc(at) >= start and is_off_hours(at, zone)),
                      "demandes de cotation transmises hors heures")
        asked = (await db.execute(select(AppointmentRequest.created_at).where(
            AppointmentRequest.tenant_id == tenant.id, AppointmentRequest.created_at >= prev_start,
            AppointmentRequest.created_at < end))).scalars().all()
        # Contrat souscrit = issue « souscrit » d'un rendez-vous OU demande passée à « Souscrit » (un client par période).
        won = (await db.execute(select(AppointmentRequest.customer_id, AppointmentRequest.outcome_at).where(
            AppointmentRequest.tenant_id == tenant.id, AppointmentRequest.outcome == OUTCOME_SOLD,
            AppointmentRequest.outcome_at >= prev_start, AppointmentRequest.outcome_at < end))).all()
        won += (await db.execute(select(QuoteRequest.customer_id, QuoteRequest.closed_at).where(
            QuoteRequest.tenant_id == tenant.id, QuoteRequest.status == STATUS_WON,
            QuoteRequest.closed_at >= prev_start, QuoteRequest.closed_at < end))).all()
        sold_now = len({c for c, at in won if as_utc(at) >= start})
        sold_prev = len({c for c, at in won if as_utc(at) < start})
        tiles = [tiles[0], tile("Demandes de cotation", *split(submitted)), tile("Rendez-vous et appels", *split(asked)),
                 tile("Contrats souscrits", sold_now, sold_prev, declared=True)]
        obtained_label = "Cotations ou rendez-vous"
        funnel_title, funnel_sub = "Vos résultats", "Du premier message au contrat, pour les demandes de la période"
    elif rdv:
        from app.models.appointment_request import AppointmentRequest

        from app.models.appointment_request import OUTCOME_SOLD

        asked = (await db.execute(select(AppointmentRequest.created_at).where(
            AppointmentRequest.tenant_id == tenant.id, AppointmentRequest.created_at >= prev_start,
            AppointmentRequest.created_at < end))).scalars().all()
        sold = (await db.execute(select(AppointmentRequest.outcome_at).where(
            AppointmentRequest.tenant_id == tenant.id, AppointmentRequest.outcome == OUTCOME_SOLD,
            AppointmentRequest.outcome_at >= prev_start, AppointmentRequest.outcome_at < end))).scalars().all()
        off_result = (sum(1 for at in asked if as_utc(at) >= start and is_off_hours(at, zone)), "rendez-vous demandés hors heures")
        tiles = [tiles[0], tiles[1], tile("Rendez-vous demandés", *split(asked)),
                 tile("Véhicules vendus", *split(sold), declared=True)]
        obtained_label = "Rendez-vous obtenus"
        funnel_title, funnel_sub = "Vos résultats", "Du premier message à la vente, pour les rendez-vous demandés sur la période"
    else:
        from app.models.order import Order, OrderStatus

        orders = (await db.execute(select(Order).where(Order.tenant_id == tenant.id, Order.created_at >= prev_start))).scalars().all()
        off_result = (sum(1 for o in orders if as_utc(o.created_at) >= start and as_utc(o.created_at) < end
                          and is_off_hours(o.created_at, zone)), "commandes passées hors heures")

        def paid_between(a, b):
            return [o for o in orders if o.status == OrderStatus.PAID and a <= as_utc(o.paid_at or o.created_at) < b]

        paid_now, paid_prev = paid_between(start, end), paid_between(prev_start, start)
        amount = float(sum(float(o.total_amount) for o in paid_now))
        amount_prev = float(sum(float(o.total_amount) for o in paid_prev))
        tiles += [tile("Encaissé" + (f" ({tenant.currency})" if tenant.currency else ""), amount, amount_prev, declared=True),
                  tile("Commandes payées", len(paid_now), len(paid_prev), declared=True)]
        # Parcours : commandes et paiements comptés comme les tuiles (paiement déclaré sur la période).
        pending = [o for o in orders if o.status == OrderStatus.PENDING and start <= as_utc(o.created_at) < end]
        detail = f"{money(amount, tenant.currency)} déclarés"
        if pending:
            detail += f" · {len(pending)} en attente ({money(sum(float(o.total_amount) for o in pending), tenant.currency)})"
        opportunities = next((f["value"] for f in home["funnel"] if f["key"] == "opportunities"), 0)
        funnel = [("Conversations", len(conversations), ""), ("Opportunités de vente", opportunities, ""),
                  ("Commandes", sum(1 for o in orders if start <= as_utc(o.created_at) < end), ""),
                  ("Payées", len(paid_now), detail)]
        funnel_title, funnel_sub = "Vos ventes", "Du premier message au paiement"

    # --- Ce que demandent les clients, réponses aux objections ---------------------------------------------
    from app.services.signal_service import signals_summary
    from app.services.strategy_service import strategies_summary

    signals = await signals_summary(db, tenant.id, days=days, now=end, business_type=tenant.business_type)
    intents = [(i["label"], i["messages"], "") for i in signals["intents"] if i["code"] not in SKIPPED_INTENTS][:INTENTS_SHOWN]
    strategies = (await strategies_summary(db, tenant.id, days=days, now=end, business_type=tenant.business_type))["strategies"]
    objections = []
    for r in sorted(strategies, key=lambda r: -r["uses"])[:ROWS_SHOWN]:
        if rdv:
            total, part = r.get("conversations", 0), r.get("appointments", 0)
            rate = f"{round(r['appointment_rate_pct'])} %" if r.get("enough_data") and r.get("appointment_rate_pct") is not None else ""
        else:
            total, part = r.get("terminated", 0) or r.get("opportunities", 0), r.get("paid", 0)
            rate = f"{round(r['conversion_rate_pct'])} %" if r.get("enough_data") and r.get("conversion_rate_pct") is not None else ""
        if total:
            objections.append((r["label"], total, part, rate))

    # --- Sources et commerciaux ----------------------------------------------------------------------------
    from app.services.acquisition import commercials_summary, sources_summary

    sources_data = await sources_summary(db, tenant.id, start, end)
    sources = []
    for r in sources_data["channels"][:ROWS_SHOWN]:
        if insurance:
            detail = _plural(r["appointments"], "rendez-vous", "rendez-vous") + " · " + _plural(r["vehicles_sold"], "contrat", "contrats")
        elif rdv:
            detail = _plural(r["appointments"], "rendez-vous", "rendez-vous") + " · " + _plural(r["vehicles_sold"], "vendu", "vendus")
        else:
            detail = _plural(r["paid_orders"], "vente", "ventes") + (f" · {money(r['revenue'], tenant.currency)}" if r["revenue"] else "")
        sources.append((r["label"], r["customers"], detail))
    commercials = []
    if rdv:
        for r in (await commercials_summary(db, tenant.id, start, end))[:ROWS_SHOWN + 3]:
            if r["prospects"] or r["appointments"]:
                won = _plural(r["vehicles_sold"], "contrat", "contrats") if insurance else _plural(r["vehicles_sold"], "vendu", "vendus")
                commercials.append((r["name"], r["prospects"], f"{_plural(r['appointments'], 'rendez-vous', 'rendez-vous')} · {won}"))

    # --- À valider (déclaré par le commerçant) et mois qui vient ---------------------------------------------
    to_validate = await _to_validate(db, tenant, end, rdv, insurance, base)
    todo = await _todo(db, tenant, end, zone, rdv, insurance)

    # --- Bob ne s'arrête jamais : présence, délai de réponse, temps libéré ---------------------------------
    pause_on = bob_pause.pause_starts_on(tenant.paid_until) if tenant.paid_until else None
    first_day, last_day = start.astimezone(zone).date(), (end - timedelta(microseconds=1)).astimezone(zone).date()
    local_days = [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]
    present = sum(1 for d in local_days if pause_on is None or d < pause_on) if tenant.active else 0
    weekend = sum(1 for d in local_days if d.weekday() >= 5 and (pause_on is None or d < pause_on))
    presence = [(f"{present} / {len(local_days)}", f"jours de présence, dont {weekend} de week-end"),
                ("24 h / 24", f"{number(present * 24)} heures de présence")]
    if delays:
        average = sum(delays) / len(delays)
        presence.append((f"{round(average)} s" if average < 90 else duration(round(average / 60)), "pour répondre, en moyenne"))
    saved = len(ai_msgs) * MINUTES_PER_REPLY
    relay = max(1, round(present * 24 / HOURS_PER_PERSON)) if present else 0
    savings = [("Messages auxquels Bob a répondu seul", number(len(ai_msgs))),
               (f"Temps de réponse évité ({MINUTES_PER_REPLY} min par message)", duration(saved))]
    if rdv and asked:
        count = sum(1 for at in asked if as_utc(at) >= start)
        if count:
            savings.append(("Rendez-vous obtenus sans déplacement", _plural(count, "rendez-vous", "rendez-vous")))
    work_days = round(saved / 60 / 7)
    compare = (f"≈ {_plural(work_days, 'journée', 'journées')} de travail" if work_days else "")
    if relay > 1:
        compare += (" · " if compare else "") + f"pour la même présence 24 h / 24, il faudrait {relay} personnes en relais"
    bob = {
        "sentence": (f"Pas de sommeil, pas de week-end, pas de congé : Bob a tenu votre accueil WhatsApp chaque jour, "
                     f"et ce rapport est arrivé à l'heure. Il prend le relais quand votre équipe se repose et lui laisse "
                     f"les clients prêts à {'souscrire' if insurance else 'acheter'}."),
        "presence": presence, "savings": savings, "value_title": "Temps libéré", "value": duration(saved),
        "compare": compare,
        "method": (f"Estimation : chaque réponse de Bob compte {MINUTES_PER_REPLY} minutes (le temps moyen pour répondre "
                   f"à un message) ; une personne à temps plein travaille {HOURS_PER_PERSON} heures par mois."),
    }

    night = {
        "replies": off_replies, "hours": hours,
        "sentence": (f"Le soir, la nuit et le week-end, votre {place} est fermé{'e' if place in ('boutique', 'concession') else ''}. "
                     "Bob, lui, a répondu tout de suite. Sans lui, ces clients auraient attendu le lendemain, "
                     f"avec le risque qu'ils {'aillent voir ailleurs' if rdv else 'achètent ailleurs'}."),
        "stats": [(len(off_customers), "clients ont écrit hors heures"), (new_off, "nouveaux clients arrivés hors heures"),
                  (off_result[0], off_result[1])],
    }

    daily_dates = [d for d in local_days]
    daily = [len(per_day.get(d, ())) for d in daily_dates]
    best = max(range(len(daily)), key=lambda i: daily[i]) if any(daily) else None
    weekdays = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    best_day = f"{weekdays[daily_dates[best].weekday()]} {short_date(daily_dates[best], False)}" if best is not None else ""

    headline = (f"{_plural(len(conversations), 'conversation', 'conversations')}"
                + (f", dont {_plural(off_replies, 'réponse', 'réponses')} hors heures" if off_replies else "")) \
        if conversations else "Votre mois avec Bob"
    data = {
        "subject": f"Votre mois avec Bob : {headline}" if conversations else "Votre mois avec Bob",
        "headline": headline, "shop": tenant.name, "period": period_label(start, end, zone),
        "sector": sector, "start": start, "end": end, "days": days,
        "tiles": tiles, "tiles_sub": "Comparé à la période précédente" if comparable else "Votre première période avec Bob",
        "night": night, "bob": bob, "to_validate": to_validate,
        "daily": daily, "daily_dates": [d.day for d in daily_dates],
        "weekend_days": {i for i, d in enumerate(daily_dates) if d.weekday() >= 5}, "best_day": best_day,
        "funnel_title": funnel_title, "funnel_sub": funnel_sub, "funnel": funnel,
        "intents": intents, "objections": objections, "obtained_label": obtained_label,
        "objections_sub": "Partie foncée : ce que la réponse a obtenu. Taux affiché à partir de 20 conversations.",
        "sources": sources, "commercials": commercials, "todo": todo,
        "advice": advice(objections, sources, len(conversations), len(off_customers), len({owner_of.get(m.conversation_id) for m in customer_msgs} - {None}),
                         rdv, insurance),
        "url": base + "?tab=reports",
        "renewal": _renewal_text(tenant) if renewal else None,
        "renewal_short": (f"Votre abonnement se termine le {short_date(tenant.paid_until)}"
                          if renewal and tenant.paid_until else None),
        "footer": [f"Vous recevez ce rapport car vous êtes administrateur de {tenant.name} sur Bob.",
                   f"Heures du pays de votre {place}. Bureau : {OFFICE_START} h – {OFFICE_END} h en semaine. "
                   "Les demandes de vos clients sont déduites par une IA : ce sont des indications."],
    }
    return data


def _plural(n: int, one: str, many: str) -> str:
    return f"{number(n)} {one if n <= 1 else many}"


def _renewal_text(tenant) -> str | None:
    from app.services.bob_pause import french_date, pause_starts_on

    if not tenant.paid_until:
        return None
    return (f"Votre abonnement est réglé jusqu'au {french_date(tenant.paid_until)}. Pour que Bob continue de répondre à vos "
            f"clients sans interruption, pensez à le renouveler avant cette date : sans renouvellement, Bob se mettra en "
            f"pause le {french_date(pause_starts_on(tenant.paid_until))}. Pour renouveler ou pour toute question, "
            "répondez simplement à cet email.")


async def _to_validate(db, tenant, end, rdv: bool, insurance: bool, base: str) -> dict:
    items = []
    if rdv:
        from app.models.appointment_request import AppointmentRequest

        past = (await db.execute(select(AppointmentRequest).where(
            AppointmentRequest.tenant_id == tenant.id, AppointmentRequest.scheduled_at.is_not(None),
            AppointmentRequest.scheduled_at < end, AppointmentRequest.outcome.is_(None),
            AppointmentRequest.status == "CONFIRMED"))).scalars().all()
        if past:
            what = "contrat souscrit" if insurance else "vendu"
            items.append((_plural(len(past), "rendez-vous passé", "rendez-vous passés"),
                          f"sans issue indiquée ({what}, à relancer, pas intéressé, absent)"))
        url = base + "?tab=appointments"
        if insurance:
            from app.models.quote_request import QuoteRequest
            from app.services.insurance import STATUS_HANDLED, STATUS_PROPOSAL

            stale = (await db.execute(select(QuoteRequest).where(
                QuoteRequest.tenant_id == tenant.id, QuoteRequest.status.in_((STATUS_HANDLED, STATUS_PROPOSAL))))).scalars().all()
            old = [q for q in stale if as_utc(q.proposal_sent_at or q.handled_at or q.submitted_at or q.created_at) < end - timedelta(days=30)]
            if old:
                items.append((_plural(len(old), "cotation", "cotations"), "sans suite depuis plus de 30 jours : souscrit ou perdu ?"))
                if not past:
                    url = base + "?tab=quotes"
    else:
        from app.models.order import Order, OrderStatus

        pending = (await db.execute(select(Order).where(Order.tenant_id == tenant.id, Order.status == OrderStatus.PENDING))).scalars().all()
        if pending:
            items.append((_plural(len(pending), "commande", "commandes"),
                          "en attente : si vous avez été payé, cliquez « Paiement reçu »"))
            old = [o for o in pending if as_utc(o.created_at) < end - timedelta(days=7)]
            if old:
                items.append((_plural(len(old), "commande", "commandes"), "en attente depuis plus de 7 jours : payées ou à annuler ?"))
        url = base + "?tab=orders"
    return {"items": items, "url": url}


async def _todo(db, tenant, end, zone, rdv: bool, insurance: bool) -> list[str]:
    from app.models.conversation import Conversation, ConversationStatus

    todo = []
    waiting = len((await db.execute(select(Conversation.id).where(
        Conversation.tenant_id == tenant.id, Conversation.status == ConversationStatus.WAITING_HUMAN))).all())
    if waiting:
        todo.append(f"{_plural(waiting, 'client attend', 'clients attendent')} la réponse d'un humain")
    if rdv:
        from app.models.appointment_request import AppointmentRequest

        rows = (await db.execute(select(AppointmentRequest).where(AppointmentRequest.tenant_id == tenant.id))).scalars().all()
        to_confirm = sum(1 for a in rows if a.status == "REQUESTED")
        coming = sum(1 for a in rows if a.status == "CONFIRMED" and a.scheduled_at and end <= as_utc(a.scheduled_at) < end + timedelta(days=7))
        if to_confirm:
            todo.append(f"{_plural(to_confirm, 'rendez-vous', 'rendez-vous')} à confirmer")
        if coming:
            todo.append(f"{_plural(coming, 'rendez-vous prévu', 'rendez-vous prévus')} dans les 7 prochains jours")
    if insurance:
        from app.models.insurance_contract import InsuranceContract
        from app.models.quote_request import QuoteRequest
        from app.services.insurance import STATUS_SUBMITTED

        today = as_utc(end).astimezone(zone).date()
        due = len((await db.execute(select(InsuranceContract.id).where(
            InsuranceContract.tenant_id == tenant.id, InsuranceContract.status == "ACTIVE",
            InsuranceContract.expires_on >= today, InsuranceContract.expires_on <= today + timedelta(days=30)))).all())
        if due:
            todo.append(f"{_plural(due, 'contrat arrive', 'contrats arrivent')} à échéance dans les 30 prochains jours")
        quotes = len((await db.execute(select(QuoteRequest.id).where(
            QuoteRequest.tenant_id == tenant.id, QuoteRequest.status == STATUS_SUBMITTED))).all())
        if quotes:
            todo.append(f"{_plural(quotes, 'demande', 'demandes')} de cotation à prendre en charge")
    if not rdv:
        from app.models.followup_settings import TenantFollowupSettings as FollowupSettings

        offer = (await db.execute(select(FollowupSettings).where(FollowupSettings.tenant_id == tenant.id))).scalar_one_or_none()
        today = as_utc(end).astimezone(zone).date()
        if offer is not None and offer.offer_text and offer.offer_ends_on and offer.offer_ends_on < today:
            todo.append(f"Votre offre du moment est terminée depuis le {short_date(offer.offer_ends_on)} : pensez à en saisir une nouvelle")
    return todo


def advice(objections: list, sources: list, conversations: int, off_customers: int, customers: int,
           rdv: bool, insurance: bool) -> str | None:
    """Un seul conseil, tiré de règles fixes (jamais de l'IA) ; aucun si aucune règle ne s'applique."""
    if conversations == 0:
        return ("Aucun client ne vous a écrit sur cette période : affichez votre lien et votre QR code WhatsApp "
                "partout où vos clients passent (kit de lancement, page Clients).")
    rated = [o for o in objections if o[1] >= 5]
    if len(rated) >= 2:
        best = max(rated, key=lambda o: o[2] / o[1])
        others = [o for o in rated if o is not best]
        other_rate = sum(o[2] for o in others) / max(1, sum(o[1] for o in others))
        best_rate = best[2] / best[1]
        if best_rate >= 0.2 and best_rate >= 2 * other_rate:
            won = "une cotation ou un rendez-vous" if insurance else "un rendez-vous" if rdv else "une vente"
            return (f"« {best[0]} » obtient {won} {_ratio(best[2], best[1])} : deux fois plus que vos autres "
                    "réponses aux objections. Gardez-la activée.")
    if customers and off_customers / customers >= 0.4:
        share = round(off_customers / customers * 100)
        return (f"{share} % de vos clients vous écrivent le soir ou le week-end : affichez votre lien WhatsApp "
                "(kit de lancement) là où ils vous cherchent à ces heures-là, sur vos réseaux et vos statuts.")
    return None


def _ratio(part: int, total: int) -> str:
    rate = part / total if total else 0
    if rate >= 0.45:
        return "une fois sur deux"
    if rate >= 0.3:
        return "une fois sur trois"
    return f"dans {round(rate * 100)} % des cas"


# --- Envoi ----------------------------------------------------------------------------------------------

async def recipients(db, tenant) -> list[tuple[str, str | None]]:
    """(email, prénom) du propriétaire et des administrateurs actifs, plus l'adresse de la boutique."""
    from app.models.user import User

    users = (await db.execute(select(User).where(
        User.tenant_id == tenant.id, User.active.is_(True), User.role.in_(REPORT_ROLES)))).scalars().all()
    seen, out = set(), []
    for u in users:
        if u.email and u.email.lower() not in seen:
            seen.add(u.email.lower())
            out.append((u.email, (u.full_name or "").split()[0] if (u.full_name or "").strip() else None))
    if tenant.email and tenant.email.lower() not in seen:
        out.append((tenant.email, None))
    return out


def render(data: dict, first_name: str | None) -> tuple[str, str, str]:
    """(sujet, texte, html) pour un destinataire."""
    from app.services import report_email

    personal = {**data, "first_name": first_name}
    return data["subject"], report_email.text(personal), report_email.report(personal)


async def send_report(db, tenant, now: datetime | None = None, send=None, renewal: bool = False,
                      only_to: tuple[str, str | None] | None = None, reply_to: str | None = None) -> int:
    """Envoie le rapport ; renvoie le nombre d'emails partis. Un envoi automatique note la période comptée."""
    if send is None:
        from app.services.email_service import send_email as send
    now = as_utc(now or datetime.now(timezone.utc))
    start, end = report_window(tenant, now)
    data = await build(db, tenant, start, end, renewal=renewal)
    sent = 0
    for email, first_name in ([only_to] if only_to else await recipients(db, tenant)):
        subject, body, html = render(data, first_name)
        try:
            ok = send(to=email, subject=subject, body=body, from_name="Bob", reply_to=reply_to, html=html)
        except Exception:  # noqa: BLE001 — un destinataire en échec ne bloque pas les autres
            logger.warning("Rapport mensuel impossible à envoyer (boutique %s)", tenant.id)
            ok = False
        sent += 1 if ok else 0
    if sent and only_to is None:
        tenant.report_sent_at, tenant.report_period_end = now, end
    return sent


def monthly_due(tenant, now: datetime) -> bool:
    """Rapport de début de mois : du 1er au 3, après 8 h (heure du pays), sans échéance proche, 20 jours d'écart."""
    if tenant is None or not tenant.active or tenant.is_demo:
        return False
    local = as_utc(now).astimezone(tenant_zone(tenant))
    if local.day > 3 or local.hour < OFFICE_START:
        return False
    if tenant.paid_until is not None and (tenant.paid_until - local.date()).days <= RENEWAL_WINDOW_DAYS:
        return False  # le rapport part avec l'email d'échéance (7 jours avant), ou la boutique est en pause
    if tenant.created_at is not None and as_utc(now) - as_utc(tenant.created_at) < timedelta(days=MIN_DAYS):
        return False
    return tenant.report_sent_at is None or as_utc(now) - as_utc(tenant.report_sent_at) >= MONTHLY_GAP


def next_send_on(tenant, now: datetime) -> date | None:
    """Date du prochain rapport automatique (affichée dans la page Rapports)."""
    if not tenant.active or tenant.is_demo:
        return None
    from app.services.bob_pause import WARNING_DAYS

    today = as_utc(now).astimezone(tenant_zone(tenant)).date()
    if tenant.paid_until is not None:
        warning = tenant.paid_until - timedelta(days=WARNING_DAYS)
        days_left = (tenant.paid_until - today).days
        if days_left < 0:
            return None
        if days_left <= RENEWAL_WINDOW_DAYS:
            return max(warning, today)
    first = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    if today.day <= 3 and (tenant.report_sent_at is None or as_utc(now) - as_utc(tenant.report_sent_at) >= MONTHLY_GAP):
        first = today
    if tenant.paid_until is not None and (tenant.paid_until - first).days <= RENEWAL_WINDOW_DAYS:
        return tenant.paid_until - timedelta(days=WARNING_DAYS)
    return first
