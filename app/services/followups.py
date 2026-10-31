"""
Lot 99 — boutons sur les messages que Bob envoie de lui-même (validé le 31/10) :
- relance d'une commande non payée : « J'ai payé », « Une question », « Annuler » (avec confirmation ; l'annulation
  elle-même reste faite par un humain, dans le tableau de bord) ;
- rappel de rendez-vous la veille : « Je confirme », « Déplacer » (créneaux libres, même outil que Bob), « Annuler »
  (avec confirmation, même outil que Bob) ;
- livraison : en route → « Suivre ma commande », « Un problème » ; livrée → « Bien reçue », « Pas reçue », « Racheter ».
Boutons seulement quand le parcours à boutons s'applique (sinon le texte part comme avant) ; aucun appel d'IA.

Identifiants (préfixe « g: ») : g:op / g:oq / g:ox / g:oxy / g:oxn :<commande> ; g:rk / g:rm / g:rn / g:rny / g:rnn :<rdv> ;
g:rs:<rdv>:<AAAAMMJJHHMM|x> ; g:dp / g:dk / g:dn :<commande> ; g:s (suivi) et g:cat (catalogue) existent déjà.
"""
import re
import uuid

PREFIX = "g:"
ACTIONS = {"op", "oq", "ox", "oxy", "oxn", "rk", "rm", "rn", "rny", "rnn", "rs", "dp", "dk", "dn",
           "pi", "ai", "ar"}  # lot 100 : « Pas intéressé » (alerte de bien), réponses après le rendez-vous


def _applies(tenant) -> bool:
    from app.services.guided_flow import applies

    return applies(tenant)


def order_buttons(tenant, order) -> list | None:
    if not _applies(tenant):
        return None
    return [(f"{PREFIX}op:{order.id}", "J'ai payé"), (f"{PREFIX}oq:{order.id}", "Une question"),
            (f"{PREFIX}ox:{order.id}", "Annuler")]


def appointment_buttons(tenant, appointment) -> list | None:
    if not _applies(tenant):
        return None
    return [(f"{PREFIX}rk:{appointment.id}", "Je confirme"), (f"{PREFIX}rm:{appointment.id}", "Déplacer"),
            (f"{PREFIX}rn:{appointment.id}", "Annuler")]


def delivery_buttons(tenant, order, step: str) -> list | None:
    if not _applies(tenant):
        return None
    if step == "SHIPPED":
        return [(f"{PREFIX}s", "Suivre ma commande"), (f"{PREFIX}dp:{order.id}", "Un problème")]
    return [(f"{PREFIX}dk:{order.id}", "Bien reçue"), (f"{PREFIX}dn:{order.id}", "Pas reçue"), (f"{PREFIX}cat", "Racheter")]


# --- Lot 100 : rappel d'échéance, alerte « nouveau bien », après le rendez-vous, confirmation, reçu ----------------

def contract_buttons(tenant, contract) -> list | None:
    if not _applies(tenant):
        return None
    return [(f"{PREFIX}mr:{contract.id}:R", "Le renouveler"), (f"{PREFIX}mr:{contract.id}:M", "Le modifier"),
            (f"{PREFIX}human", "Un conseiller")]


def property_buttons(tenant, product) -> list | None:
    if not _applies(tenant):
        return None
    return [(f"{PREFIX}d:{product.id}", "Voir le bien"), (f"{PREFIX}vi:{product.id}", "Demander une visite"),
            (f"{PREFIX}pi:{product.id}", "Pas intéressé")]


def after_visit_buttons(tenant, appointment) -> list | None:
    if not _applies(tenant):
        return None
    return [(f"{PREFIX}ai:{appointment.id}", "Ça m'intéresse"), (f"{PREFIX}ar:{appointment.id}", "Je réfléchis"),
            (f"{PREFIX}human", "Un conseiller")]


def booking_buttons(tenant, appointment) -> list | None:
    if not _applies(tenant):
        return None
    return [(f"{PREFIX}rm:{appointment.id}", "Déplacer"), (f"{PREFIX}rn:{appointment.id}", "Annuler")]


def receipt_buttons(tenant) -> list | None:
    if not _applies(tenant):
        return None
    return [(f"{PREFIX}cat", "Voir le catalogue"), (f"{PREFIX}s", "Suivre ma commande")]


def _you(tu, vous, toi):
    return toi if tu else vous


def _turn(actions=None, **kw):
    from app.services.guided_flow import GuidedTurn

    return GuidedTurn(actions or [], **kw)


def _text(body, **extra):
    return {"type": "text", "body": body, **extra}


def _uuid(raw):
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _note(db, tenant, conversation, content):
    from app.models.conversation import Message, MessageSender

    if conversation is not None:
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                       message_type="client_action", content=content))


async def _order(db, tenant, customer, raw):
    from app.models.order import Order

    order = await db.get(Order, _uuid(raw)) if _uuid(raw) else None
    return order if order is not None and order.tenant_id == tenant.id and order.customer_id == customer.id else None


def _ref(order) -> str:
    return str(order.id)[:8].upper()


# --- Commande non payée -------------------------------------------------------------------------------------------

async def _order_tap(db, tenant, tu, customer, conversation, action, raw):
    from app.models.order import OrderStatus
    from app.services.order_emails import _money

    order = await _order(db, tenant, customer, raw)
    if order is None:
        return None
    if action in ("op", "oq", "ox", "oxy") and order.status != OrderStatus.PENDING:
        return _turn([_text(_you(tu, f"La commande n° {_ref(order)} n'est plus en attente de paiement.",
                                 f"La commande n° {_ref(order)} n'est plus en attente de paiement."))])
    if action == "op":
        _note(db, tenant, conversation, f"Le client indique avoir payé la commande n° {_ref(order)}")
        return _turn([_text(_you(tu, "Merci ! Envoyez ici la capture d'écran du paiement : la boutique confirme et vous "
                                     "envoie votre reçu.",
                                 "Merci ! Envoie ici la capture d'écran du paiement : la boutique confirme et t'envoie ton reçu."))])
    if action == "oq":
        return _turn(handoff=True, handoff_reason=f"Question sur la commande n° {_ref(order)}",
                     handoff_text=_you(tu, "Je préviens la boutique : elle vous répond ici.",
                                       "Je préviens la boutique : elle te répond ici."))
    if action == "ox":
        return _turn([{"type": "buttons", "body": f"Annuler la commande n° {_ref(order)} "
                                                  f"({_money(order.total_amount, order.currency)}) ?",
                       "buttons": [(f"{PREFIX}oxy:{order.id}", "Oui, annuler"), (f"{PREFIX}oxn:{order.id}", "Non, la garder")]}])
    if action == "oxy":
        return _turn(handoff=True,
                     handoff_reason=f"Le client demande l'annulation de la commande n° {_ref(order)} "
                                    f"({_money(order.total_amount, order.currency)})",
                     handoff_text=_you(tu, "C'est noté : la boutique annule la commande et vous le confirme ici.",
                                       "C'est noté : la boutique annule la commande et te le confirme ici."))
    if action == "oxn":
        return _turn([_text(_you(tu, "Parfait, votre commande reste enregistrée.", "Parfait, ta commande reste enregistrée."))])
    return None


# --- Livraison -----------------------------------------------------------------------------------------------------

async def _delivery_tap(db, tenant, tu, customer, conversation, action, raw):
    order = await _order(db, tenant, customer, raw)
    if order is None:
        return None
    if action == "dk":
        _note(db, tenant, conversation, f"Le client confirme avoir reçu la commande n° {_ref(order)}")
        return _turn([_text(_you(tu, "Merci ! Bonne journée, et à bientôt.", "Merci ! Bonne journée, et à bientôt."))])
    reason = (f"Commande n° {_ref(order)} marquée livrée mais pas reçue par le client" if action == "dn"
              else f"Problème de livraison signalé : commande n° {_ref(order)}")
    return _turn(handoff=True, handoff_reason=reason,
                 handoff_text=_you(tu, "Je préviens la boutique tout de suite : elle vous répond ici.",
                                   "Je préviens la boutique tout de suite : elle te répond ici."))


# --- Rendez-vous ---------------------------------------------------------------------------------------------------

def _tools(db, tenant, customer, conversation):
    from app.agents.tools import ToolExecutor

    return ToolExecutor(db, tenant.id, conversation, customer.id, business_type=tenant.business_type)


async def _change_alerts(db, tenant, customer, conversation, tools) -> list:
    from app.services.appointment_service import staff_change_email
    from app.services.handoff_service import conversation_link, customer_display_name
    from app.services.local_time import tenant_zone

    return [staff_change_email(appointment, customer_display_name(customer), tenant_zone(tenant),
                               conversation_link(conversation), previous)
            for appointment, previous in tools.change_outbox]


async def _slot_rows(db, tenant, appointment, tu):
    from datetime import datetime, timezone

    from app.services import booking
    from app.services.local_time import format_local, tenant_zone

    settings = await booking.load_settings(db, tenant.id)
    if not booking.booking_enabled(settings):
        return []
    zone = tenant_zone(tenant)
    free = await booking.free_slots(db, tenant, settings, zone, datetime.now(timezone.utc))
    rows = [(f"{PREFIX}rs:{appointment.id}:{s.strftime('%Y%m%d%H%M')}", format_local(s.astimezone(timezone.utc), zone)[:24], "Libre")
            for s in free]
    if rows:
        rows.append((f"{PREFIX}rs:{appointment.id}:x", "Autre moment", _you(tu, "Un conseiller vous recontacte",
                                                                             "Un conseiller te recontacte")))
    return rows


async def _appointment_tap(db, tenant, tu, customer, conversation, action, parts):
    from app.services.appointment_service import subject_phrase

    tools = _tools(db, tenant, customer, conversation)
    appointment = await tools._my_appointment(parts[1])
    if appointment is None:
        return _turn([_text(_you(tu, "Ce rendez-vous n'est plus prévu. Écrivez-moi si vous voulez en prendre un autre.",
                                 "Ce rendez-vous n'est plus prévu. Écris-moi si tu veux en prendre un autre."))])
    what = subject_phrase(appointment)
    if action == "rk":
        _note(db, tenant, conversation, f"Le client a confirmé sa présence : {what}")
        return _turn([_text(_you(tu, "Parfait, c'est noté. À bientôt !", "Parfait, c'est noté. À bientôt !"))])
    if action == "rm":
        rows = await _slot_rows(db, tenant, appointment, tu)
        if not rows:
            return _turn(handoff=True, handoff_reason=f"Le client souhaite déplacer son rendez-vous : {what}",
                         handoff_text=_you(tu, "C'est noté : un conseiller vous propose une autre date ici.",
                                           "C'est noté : un conseiller te propose une autre date ici."))
        return _turn([{"type": "list", "body": _you(tu, "Quand êtes-vous disponible ?", "Quand es-tu disponible ?"),
                       "button": "Choisir un créneau", "rows": rows}])
    if action == "rs" and len(parts) == 3:
        if parts[2] == "x":
            return _turn(handoff=True, handoff_reason=f"Le client souhaite déplacer son rendez-vous : {what}",
                         handoff_text=_you(tu, "C'est noté : un conseiller vous propose une autre date ici.",
                                           "C'est noté : un conseiller te propose une autre date ici."))
        if not re.fullmatch(r"\d{12}", parts[2]):
            return None
        raw = parts[2]
        result = await tools._tool_reschedule_my_appointment(
            {"appointment_id": str(appointment.id), "slot": f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T{raw[8:10]}:{raw[10:12]}"})
        if result.get("status") == "rescheduled":
            return _turn([_text(text, message_type="appointment_changed") for text in tools.message_outbox],
                         alerts=await _change_alerts(db, tenant, customer, conversation, tools))
        rows = await _slot_rows(db, tenant, appointment, tu)
        if rows:
            return _turn([{"type": "list", "body": "Ce créneau vient d'être pris. " + _you(tu, "Quand êtes-vous disponible ?",
                                                                                         "Quand es-tu disponible ?"),
                           "button": "Choisir un créneau", "rows": rows}])
        return _turn(handoff=True, handoff_reason=f"Le client souhaite déplacer son rendez-vous : {what}",
                     handoff_text=_you(tu, "C'est noté : un conseiller vous propose une autre date ici.",
                                       "C'est noté : un conseiller te propose une autre date ici."))
    if action == "rn":
        return _turn([{"type": "buttons", "body": f"Annuler votre rendez-vous ({what}) ?" if not tu else f"Annuler ton rendez-vous ({what}) ?",
                       "buttons": [(f"{PREFIX}rny:{appointment.id}", "Oui, annuler"), (f"{PREFIX}rm:{appointment.id}", "Plutôt déplacer"),
                                   (f"{PREFIX}rnn:{appointment.id}", "Non, je viens")]}])
    if action == "rny":
        result = await tools._tool_cancel_my_appointment({"appointment_id": str(appointment.id)})
        if result.get("status") == "cancelled":
            return _turn([_text(text, message_type="appointment_changed") for text in tools.message_outbox],
                         alerts=await _change_alerts(db, tenant, customer, conversation, tools))
        return None
    if action == "rnn":
        return _turn([_text(_you(tu, "Parfait, votre rendez-vous est maintenu. À bientôt !",
                                 "Parfait, ton rendez-vous est maintenu. À bientôt !"))])
    return None


async def _not_interested(db, tenant, tu, customer, conversation, raw):
    """« Pas intéressé » : plus aucune alerte de bien pour ce contact (il peut écrire pour en recevoir de nouveau)."""
    from datetime import datetime, timezone

    from app.models.prospect_profile import ProspectProfile
    from app.services import prospect

    profile = await prospect.load(db, tenant.id, customer.id)
    if profile is None:
        profile = ProspectProfile(tenant_id=tenant.id, customer_id=customer.id)
        db.add(profile)
    profile.real_estate = {**(profile.real_estate or {}), "alerts_stopped_at": datetime.now(timezone.utc).isoformat()}
    _note(db, tenant, conversation, "Le client ne veut plus recevoir d'alertes de biens")
    return _turn([_text(_you(tu, "C'est noté : je ne vous enverrai plus de nouveaux biens. Écrivez-moi quand vous voulez "
                                 "reprendre votre recherche.",
                             "C'est noté : je ne t'enverrai plus de nouveaux biens. Écris-moi quand tu veux reprendre ta recherche."))])


async def _after_visit(db, tenant, tu, customer, conversation, action, raw):
    from app.services.appointment_service import subject_phrase

    tools = _tools(db, tenant, customer, conversation)
    appointment = await tools._my_appointment(raw)
    what = subject_phrase(appointment) if appointment is not None else "le rendez-vous"
    if action == "ai":
        return _turn(handoff=True, handoff_reason=f"Après {what}, le client est intéressé",
                     handoff_text=_you(tu, "Très bien ! Je préviens un conseiller : il vous recontacte ici pour la suite.",
                                       "Très bien ! Je préviens un conseiller : il te recontacte ici pour la suite."))
    _note(db, tenant, conversation, f"Après {what}, le client réfléchit")
    return _turn([_text(_you(tu, "Prenez votre temps. Écrivez-moi quand vous voulez : je suis là.",
                             "Prends ton temps. Écris-moi quand tu veux : je suis là."))])


async def handle_tap(db, tenant, tu, customer, conversation, action, parts):
    """Réponse à un bouton de relance, de rappel ou de livraison ; None si ce n'est pas l'un d'eux."""
    if action not in ACTIONS or customer is None or conversation is None or len(parts) < 2:
        return None
    if action == "pi":
        return await _not_interested(db, tenant, tu, customer, conversation, parts[1])
    if action in ("ai", "ar"):
        return await _after_visit(db, tenant, tu, customer, conversation, action, parts[1])
    if action.startswith("o"):
        return await _order_tap(db, tenant, tu, customer, conversation, action, parts[1])
    if action.startswith("d"):
        return await _delivery_tap(db, tenant, tu, customer, conversation, action, parts[1])
    return await _appointment_tap(db, tenant, tu, customer, conversation, action, parts)

