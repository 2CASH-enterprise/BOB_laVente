"""
Exécution réelle des outils (section 33 : protection contre les hallucinations).

Principe absolu : cette classe est le SEUL endroit où prix, stock et existence d'un
produit sont déterminés. Le LLM ne fait jamais que lire ce qui est renvoyé ici — il
n'a aucun autre moyen d'obtenir ces informations (section 50 : le LLM comprend,
raisonne, utilise les outils, communique ; il n'est jamais la source de vérité).
"""
import json
import re
from datetime import timezone
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.delivery import Delivery
from app.models.negotiation_settings import TenantNegotiationSettings
from app.models.order import Order, OrderItem
from app.repositories.product_complement_repository import ProductComplementRepository
from app.repositories.product_repository import ProductRepository
from app.services.negotiation_service import NegotiationError, negotiate_price
from app.services.order_service import OrderCreationError, create_order
from app.services.customer_memory_service import record_product_view


PRODUCT_DESCRIPTION_MAX_CHARS = 300
MAX_IMAGES_PER_REPLY = 3


def has_sendable_photo(product) -> bool:
    """Une photo n'est envoyée sur WhatsApp que par une adresse publique sécurisée (https)."""
    return (getattr(product, "image_url", None) or "").strip().lower().startswith("https://")


# Lot 35c — mots d'une annulation EXPLICITE (« annulez », « supprimez », « plus besoin »,
# « je ne viendrai pas du tout »…). « Je ne peux plus venir lundi » n'en est pas une : c'est un report.
_EXPLICIT_CANCEL = re.compile(
    r"\bannul|\bsupprim|\bplus besoin\b|\blaiss\w* tomber\b|\boubli(?:ez|e|ons)\b|\bdu tout\b|\bd[ée]sist",
    re.IGNORECASE,
)


_AFFIRMATIVE = re.compile(r"^\s*(oui|ok|okay|d'accord|daccord|c'est ça|exactement|volontiers|oui merci|oui svp)\b[\s.!]*$", re.IGNORECASE)


def asks_explicit_cancellation(text: str | None) -> bool:
    return bool(_EXPLICIT_CANCEL.search(text or ""))


class ToolExecutor:
    def __init__(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        conversation: Conversation,
        customer_id: uuid.UUID | None = None,
        business_type: str | None = None,
    ):
        self.db = db
        # Lot 24 : les outils sont filtrés selon le type d'activité, ici aussi (verrou) et pas
        # seulement dans la liste envoyée à l'IA.
        self.business_type = business_type
        self.tenant_id = tenant_id
        self.conversation = conversation
        self.customer_id = customer_id or conversation.customer_id
        self.product_repo = ProductRepository(db)
        self.complement_repo = ProductComplementRepository(db)
        self.handoff_requested: bool = False
        self.handoff_reason: str | None = None
        # Lot 13 : décision des règles de transmission pour CE message (None = pas de règle).
        self.turn = None
        # Lot 26c : photos à envoyer après la réponse (remplies par send_product_images).
        self.pending_images: list[dict] = []
        # Lot 26d : produits vus pendant ce message et qui ont une photo envoyable (id → nom).
        self.products_with_photo: dict[str, str] = {}
        # Lot 26e : Bob a-t-il consulté au moins un produit pendant ce message ?
        self.looked_up_products = False
        # Lot 29 : rendez-vous réservés par Bob pendant ce message (confirmation fixe envoyée ensuite).
        self.booking_outbox: list = []
        # Lot 34b : messages fixes à envoyer après la réponse de Bob (ex. demande d'email).
        self.message_outbox: list[str] = []
        # Lot 35 : rendez-vous déplacés ou annulés par le client (alerte à la boutique ensuite).
        self.change_outbox: list[tuple] = []
        # Lot 35c : dernier message du client (None hors conversation : aucun verrou appliqué).
        self.incoming_text: str | None = None

    async def execute(self, tool_name: str, tool_input: dict) -> dict:
        from app.services.business_type import tool_allowed

        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return {"error": f"Outil inconnu : {tool_name}"}
        if not tool_allowed(self.business_type, tool_name):
            return {"error": f"Outil non disponible pour cette activité : {tool_name}"}
        return await handler(tool_input)

    async def _product_to_dict(self, product) -> dict:
        from app.services.vehicle import for_ai

        # Lot 15 : la description (tronquée) permet à Bob d'argumenter sur des caractéristiques
        # RÉELLES au lieu de les inventer. Le prix d'achat (cost_price) n'est jamais exposé : il
        # révélerait la marge du commerçant.
        description = (product.description or "").strip()
        if len(description) > PRODUCT_DESCRIPTION_MAX_CHARS:
            description = description[:PRODUCT_DESCRIPTION_MAX_CHARS].rstrip() + "…"
        # Lot 26d : Bob doit SAVOIR qu'une photo existe (incident du 29/09 : « pas de photo
        # disponible » alors que la 5008 en avait une). Jamais l'adresse elle-même.
        has_photo = has_sendable_photo(product)
        self.looked_up_products = True
        if has_photo:
            self.products_with_photo[str(product.id)] = product.name
        return {
            "product_id": str(product.id),
            "name": product.name,
            "description": description or None,
            "price": float(product.price),
            "currency": product.currency,
            "stock": product.stock_quantity,
            "active": product.active,
            "photo": "disponible : utilise send_product_images pour l'envoyer" if has_photo else "aucune",
            **({"vehicle": for_ai(product.vehicle)} if getattr(product, "vehicle", None) else {}),
        }

    async def _tool_search_products(self, tool_input: dict) -> dict:
        from app.services.business_type import CAR_DEALERSHIP, normalize
        from app.services.vehicle import criteria_from_query, matches

        dealership = normalize(self.business_type) == CAR_DEALERSHIP
        query = tool_input.get("query")
        criteria = {key: tool_input.get(key) for key in ("fuel", "gearbox", "min_year", "max_mileage_km", "body_type")}
        if dealership:
            # Lot 26b : « SUV », « diesel », « automatique » dans la recherche sont des critères,
            # pas des mots à trouver dans le nom du véhicule.
            query, from_words = criteria_from_query(query)
            for key, value in from_words.items():
                if criteria.get(key) is None:
                    criteria[key] = value
        filtering = dealership and any(v is not None for v in criteria.values())
        products = await self.product_repo.search(
            tenant_id=self.tenant_id,
            query=query,
            min_price=tool_input.get("min_price"),
            max_price=tool_input.get("max_price"),
            # Lot 26 : les critères véhicule se vérifient après la requête, sur un lot plus large.
            limit=500 if filtering else 10,
        )
        if filtering:
            try:
                products = [p for p in products if matches(getattr(p, "vehicle", None), **criteria)][:10]
            except (TypeError, ValueError):
                return {"error": "Critères de recherche invalides (année et kilométrage : nombres entiers)."}
        if not products:
            return {"results": [], "message": "Aucun produit trouvé pour cette recherche."}
        await record_product_view(self.db, self.tenant_id, self.customer_id, [p.id for p in products])
        return {"results": [await self._product_to_dict(p) for p in products]}

    async def _tool_check_stock(self, tool_input: dict) -> dict:
        product_id = tool_input.get("product_id")
        try:
            product = await self.product_repo.get(tenant_id=self.tenant_id, record_id=uuid.UUID(product_id))
        except (ValueError, TypeError):
            return {"error": "Identifiant produit invalide"}
        if product is None:
            return {"error": "Produit introuvable"}
        return {"product_id": str(product.id), "stock": product.stock_quantity, "active": product.active}

    async def _tool_get_product_price(self, tool_input: dict) -> dict:
        product_id = tool_input.get("product_id")
        try:
            product = await self.product_repo.get(tenant_id=self.tenant_id, record_id=uuid.UUID(product_id))
        except (ValueError, TypeError):
            return {"error": "Identifiant produit invalide"}
        if product is None:
            return {"error": "Produit introuvable"}
        return {"product_id": str(product.id), "price": float(product.price), "currency": product.currency}

    async def _tool_recommend_products(self, tool_input: dict) -> dict:
        budget = tool_input.get("budget")
        products = await self.product_repo.search(
            tenant_id=self.tenant_id,
            query=tool_input.get("customer_need", ""),
            max_price=budget,
            limit=10,
        )
        if not products:
            # Recherche large de secours : sans le terme du besoin, juste le budget (section 17)
            products = await self.product_repo.search(tenant_id=self.tenant_id, query=None, max_price=budget, limit=10)

        # Priorité aux produits en stock (section 17, 22 : max 3 propositions)
        in_stock = [p for p in products if p.stock_quantity > 0]
        pool = in_stock or products

        # Popularité réelle (point 5) : quantité vendue depuis les vraies commandes, jamais devinée.
        sales_counts = await self.product_repo.get_sales_counts(self.tenant_id, [p.id for p in pool])

        def _sort_key(p):
            budget_fit = abs(float(p.price) - float(budget)) if budget else 0.0
            popularity = -sales_counts.get(p.id, 0)  # négatif : plus vendu = mieux classé
            margin = -(float(p.price) - float(p.cost_price)) if p.cost_price is not None else 0.0
            # Avec budget : priorité à la proximité de budget, popularité en départage.
            # Sans budget : priorité à la popularité (meilleures ventes en premier), marge en départage.
            return (budget_fit, popularity, margin) if budget else (popularity, margin, budget_fit)

        pool = sorted(pool, key=_sort_key)
        top3 = pool[:3]

        if not top3:
            return {"results": [], "message": "Aucun produit ne correspond à ce besoin dans le catalogue."}
        await record_product_view(self.db, self.tenant_id, self.customer_id, [p.id for p in top3])
        return {"results": [await self._product_to_dict(p) for p in top3]}

    async def _tool_get_frequently_bought_together(self, tool_input: dict) -> dict:
        product_id = tool_input.get("product_id")
        try:
            product_uuid = uuid.UUID(product_id)
        except (ValueError, TypeError):
            return {"error": "Identifiant produit invalide"}

        products = await self.product_repo.get_frequently_bought_together(self.tenant_id, product_uuid, limit=3)
        if not products:
            return {"results": [], "message": "Pas encore assez de données de vente pour ce produit."}
        return {"results": [await self._product_to_dict(p) for p in products]}

    async def _tool_suggest_complementary_products(self, tool_input: dict) -> dict:
        product_id = tool_input.get("product_id")
        try:
            complements = await self.complement_repo.list_for_product(
                tenant_id=self.tenant_id, product_id=uuid.UUID(product_id), limit=2
            )
        except (ValueError, TypeError):
            return {"error": "Identifiant produit invalide"}

        if not complements:
            return {"results": [], "message": "Aucun produit complémentaire configuré pour cet article."}
        return {"results": [await self._product_to_dict(p) for p in complements]}

    async def _tool_handoff_to_human(self, tool_input: dict) -> dict:
        from app.services.handoff_rules import FORBID_TRANSFER

        # Verrou garanti par le code : une règle a interdit le transfert pour ce message.
        if self.turn is not None and self.turn.mode == FORBID_TRANSFER:
            self.turn.handoff_blocked = True
            return {
                "error": (
                    "Transfert non autorisé pour ce message (règle : "
                    f"{self.turn.rule_label}). Réponds toi-même au client en suivant la consigne."
                )
            }

        reason = tool_input.get("reason", "Non précisé")
        if self.turn is not None and self.turn.rule_label:
            reason = f"{reason} — décision de Bob (règle : {self.turn.rule_label})"
        else:
            reason = f"{reason} — décision de Bob"
        self.conversation.status = ConversationStatus.WAITING_HUMAN
        self.handoff_requested = True
        self.handoff_reason = reason
        self.db.add(
            Message(
                tenant_id=self.tenant_id,
                conversation_id=self.conversation.id,
                sender=MessageSender.SYSTEM,
                message_type="handoff",
                content=f"Transfert vers un humain : {reason}",
            )
        )
        await self.db.flush()
        return {"status": "handoff_registered", "reason": reason}

    async def _tool_send_product_images(self, tool_input: dict) -> dict:
        """
        Lot 26c : photos RÉELLES des produits de CETTE boutique, jamais une adresse fournie par l'IA.
        Trois photos au plus par réponse ; seules les adresses https publiques sont envoyées.
        """
        raw_ids = tool_input.get("product_ids") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return {"error": "Indique les identifiants des produits (product_ids)."}
        queued, without_photo, unknown = [], [], []
        for raw in raw_ids[:MAX_IMAGES_PER_REPLY]:
            try:
                product = await self.product_repo.get(tenant_id=self.tenant_id, record_id=uuid.UUID(str(raw)))
            except (ValueError, TypeError):
                product = None
            if product is None or not product.active:
                unknown.append(str(raw))
                continue
            url = (product.image_url or "").strip()
            if not has_sendable_photo(product):
                without_photo.append(product.name)
                continue
            if any(img["product_id"] == str(product.id) for img in self.pending_images):
                queued.append(product.name)
                continue
            if len(self.pending_images) >= MAX_IMAGES_PER_REPLY:
                break
            price = f"{float(product.price):,.0f}".replace(",", " ")
            self.pending_images.append({
                "product_id": str(product.id),
                "link": url,
                "caption": f"{product.name} — {price} {product.currency}",
            })
            queued.append(product.name)
        result = {"status": "photos_queued" if queued else "no_photo_sent", "photos_sent_after_reply": queued}
        if without_photo:
            result["without_photo"] = without_photo
            result["instruction"] = "Dis simplement au client que ces produits n'ont pas encore de photo."
        if unknown:
            result["unknown_products"] = unknown
        return result

    async def _booking_context(self, lock: bool = False):
        from datetime import datetime, timezone

        from app.models.tenant import Tenant
        from app.services import booking
        from app.services.local_time import tenant_zone

        tenant = await self.db.get(Tenant, self.tenant_id)
        settings = await booking.load_settings(self.db, self.tenant_id, lock=lock)
        return tenant, settings, tenant_zone(tenant), datetime.now(timezone.utc)

    async def _tool_get_available_slots(self, tool_input: dict) -> dict:
        """Lot 29 : créneaux réellement libres, calculés par le code (jamais par l'IA)."""
        from datetime import date

        from app.services import booking

        tenant, settings, zone, now = await self._booking_context()
        if not booking.booking_enabled(settings):
            return {
                "status": "no_online_booking",
                "instruction": "Pas de créneaux à proposer : ne parle pas de créneaux au client (information "
                               "interne). Demande-lui simplement quel jour et à quel moment il peut venir. Quand il "
                               "répond, utilise request_appointment sans slot. Ne dis pas que tu transmets sa demande "
                               "avant d'avoir appelé request_appointment.",
            }
        day, part, note = None, None, None
        if tool_input.get("preferred_date"):
            try:
                day = date.fromisoformat(str(tool_input["preferred_date"]))
            except ValueError:
                return {"error": "preferred_date doit être au format AAAA-MM-JJ", **booking.today_for_ai(now, zone)}
        if tool_input.get("part_of_day") in booking.PARTS_OF_DAY:
            part = tool_input["part_of_day"]
        slots = await booking.free_slots(self.db, tenant, settings, zone, now, day=day, part=part)
        if not slots and (day is not None or part is not None):
            note = "Aucun créneau libre à ce moment-là : propose plutôt ces prochains créneaux libres."
            slots = await booking.free_slots(self.db, tenant, settings, zone, now)
        result = {**booking.today_for_ai(now, zone), "slots": booking.slots_for_ai(slots, zone)}
        if note:
            result["note"] = note
        if not slots:
            result["instruction"] = ("Aucun créneau libre dans les 7 prochains jours : demande au client quel jour et "
                                     "à quel moment il peut venir, puis utilise request_appointment sans slot. Ne dis "
                                     "pas que tu transmets sa demande avant d'avoir appelé request_appointment.")
        else:
            result["instruction"] = ("Propose ces créneaux au client avec ces libellés exacts, et aucun autre. "
                                     "Quand il en choisit un, appelle request_appointment avec slot = la valeur "
                                     "« slot » de ce créneau.")
        return result

    async def _book_slot(self, tool_input: dict, kind: str) -> tuple[object | None, dict | None]:
        """
        Lot 29 : réserve et CONFIRME le créneau choisi, après l'avoir revérifié sous verrou.
        Renvoie (rendez-vous, None) ou (None, erreur pour l'IA).
        """
        from app.services import booking
        from app.services.local_time import format_local

        tenant, settings, zone, now = await self._booking_context(lock=True)
        if not booking.booking_enabled(settings):
            return None, {"error": "La réservation en ligne n'est pas activée : demande ses disponibilités au "
                                   "client et utilise request_appointment sans slot."}
        try:
            start = booking.parse_slot_id(tool_input.get("slot"), zone)
        except ValueError:
            start = None
        if start is None or not await booking.is_bookable(self.db, tenant, settings, zone, now, start):
            fresh = await booking.free_slots(self.db, tenant, settings, zone, now)
            return None, {
                "error": "Ce créneau n'est pas disponible (déjà pris, fermé ou passé).",
                "available_slots": booking.slots_for_ai(fresh, zone),
                "instruction": "Propose au client ces créneaux libres à la place.",
            }
        return (start, format_local(start, zone), now), None

    async def _tool_request_appointment(self, tool_input: dict) -> dict:
        """
        Lot 24 (concession) : enregistre la demande puis la transmet à un conseiller. Le transfert
        n'est pas soumis au verrou « transfert interdit » : une demande de rendez-vous concrète est
        toujours un motif légitime, et un rendez-vous que personne ne verrait serait perdu.

        Lot 29 : avec un créneau (slot) proposé par get_available_slots, le rendez-vous est
        confirmé tout de suite, sans transfert ; le client reçoit une confirmation fixe.
        """
        from app.models.appointment_request import APPOINTMENT_KINDS, STATUS_CONFIRMED, AppointmentRequest

        kind = tool_input.get("kind")
        availability = (tool_input.get("availability") or "").strip()
        if kind not in APPOINTMENT_KINDS:
            return {"error": "Type de rendez-vous invalide : ESSAI, VISITE ou ESTIMATION_REPRISE"}
        booked = None
        if tool_input.get("slot"):
            booked, error = await self._book_slot(tool_input, kind)
            if error:
                return error
            availability = booked[1]
        else:
            # Lot 29b — verrou : quand la concession a des créneaux libres, Bob ne peut pas
            # transmettre une demande en texte libre ; il doit proposer ces créneaux.
            from app.services import booking

            tenant, settings, zone, now = await self._booking_context()
            if booking.booking_enabled(settings):
                slots = await booking.free_slots(self.db, tenant, settings, zone, now)
                if slots:
                    return {
                        "error": "Ne transmets pas cette demande : la concession a des créneaux libres.",
                        **booking.today_for_ai(now, zone),
                        "slots": booking.slots_for_ai(slots, zone),
                        "instruction": (
                            "Propose ces créneaux au client avec ces libellés exacts (ou appelle get_available_slots "
                            "avec le jour ou le moment qu'il a indiqué). Quand il en choisit un, appelle "
                            "request_appointment avec slot = la valeur « slot » de ce créneau."
                        ),
                    }
        if not availability:
            return {"error": "Demande d'abord au client quand il est disponible."}

        product = None
        product_id = tool_input.get("product_id")
        if product_id:
            try:
                product = await self.product_repo.get(tenant_id=self.tenant_id, record_id=uuid.UUID(str(product_id)))
            except (ValueError, TypeError):
                product = None
        vehicle = (product.name if product is not None else (tool_input.get("vehicle") or "").strip()) or None
        notes = (tool_input.get("notes") or "").strip() or None

        def _text(key: str, limit: int) -> str | None:
            value = (tool_input.get(key) or "")
            value = value.strip() if isinstance(value, str) else ""
            return value[:limit] or None

        need, budget, trade_in = _text("need", 300), _text("budget", 100), _text("trade_in", 300)
        financing = tool_input.get("financing_interest")
        financing = financing if isinstance(financing, bool) else None
        # Lot 43 — la fiche prospect garde aussi ces informations (même sans autre rendez-vous ensuite).
        from app.services import prospect as prospect_service

        await prospect_service.update_profile(self.db, self.tenant_id, self.customer_id, {
            "need": need, "budget": budget, "trade_in": trade_in,
            "payment": None if financing is None else ("FINANCEMENT" if financing else None),
        })

        appointment = AppointmentRequest(
            tenant_id=self.tenant_id,
            conversation_id=self.conversation.id,
            customer_id=self.customer_id,
            product_id=product.id if product is not None else None,
            kind=kind,
            vehicle_label=vehicle[:255] if vehicle else None,
            availability=availability[:300],
            notes=notes,
            need=need,
            budget=budget,
            trade_in=trade_in,
            financing_interest=financing,
        )
        self.db.add(appointment)

        if booked is not None:
            start, label, now = booked
            appointment.status = STATUS_CONFIRMED
            appointment.scheduled_at = start.astimezone(timezone.utc)
            appointment.confirmed_at = now
            appointment.confirmed_by = "BOB"
            parts = [APPOINTMENT_KINDS[kind], vehicle, label]
            self.db.add(Message(
                tenant_id=self.tenant_id, conversation_id=self.conversation.id, sender=MessageSender.SYSTEM,
                message_type="appointment_booked",
                content="Rendez-vous confirmé par Bob : " + " — ".join(p for p in parts if p),
            ))
            await self.db.flush()
            self.booking_outbox.append(appointment)
            from app.services.contact_capture import REASON_APPOINTMENT

            instruction = (
                f"Le rendez-vous est confirmé ({label}). Un message de confirmation part automatiquement "
                "juste après ta réponse : réponds brièvement et chaleureusement, sans changer la date ni "
                "l'heure, et sans dire qu'un conseiller va recontacter le client."
            )
            ask = await self._email_ask(REASON_APPOINTMENT)  # lot 34
            return {
                "status": "appointment_confirmed",
                "when": label,
                "instruction": f"{instruction} {ask}" if ask else instruction,
            }

        details = [APPOINTMENT_KINDS[kind]]
        if vehicle:
            details.append(vehicle)
        details.append(f"disponibilités : {availability}")
        reason = "Rendez-vous à confirmer — " + " — ".join(details)
        qualification = [
            f"besoin : {need}" if need else None,
            f"budget : {budget}" if budget else None,
            f"reprise : {trade_in}" if trade_in else None,
            {True: "financement : intéressé", False: "financement : non"}.get(financing),
            f"notes : {notes}" if notes else None,
        ]
        qualification = [q for q in qualification if q]
        if qualification:
            reason += " (" + " ; ".join(qualification) + ")"
        self.conversation.status = ConversationStatus.WAITING_HUMAN
        self.handoff_requested = True
        self.handoff_reason = reason
        self.db.add(Message(
            tenant_id=self.tenant_id,
            conversation_id=self.conversation.id,
            sender=MessageSender.SYSTEM,
            message_type="handoff",
            content=f"Transfert vers un humain : {reason}",
        ))
        await self.db.flush()
        from app.services.contact_capture import REASON_APPOINTMENT

        instruction = ("Dis au client que sa demande est bien notée et qu'un conseiller va lui confirmer le "
                       "rendez-vous. Ne confirme ni date ni heure toi-même.")
        ask = await self._email_ask(REASON_APPOINTMENT)  # lot 34
        return {
            "status": "appointment_requested",
            "instruction": f"{instruction} {ask}" if ask else instruction,
        }

    # --- Lot 35 : le prospect déplace ou annule SON rendez-vous ------------------------------------

    async def _my_appointment(self, raw_id):
        """Uniquement un rendez-vous de CE client, dans CETTE boutique, non annulé."""
        from sqlalchemy import select

        from app.models.appointment_request import STATUS_CANCELLED, AppointmentRequest

        try:
            appointment_id = uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            return None
        return (await self.db.execute(select(AppointmentRequest).where(
            AppointmentRequest.id == appointment_id,
            AppointmentRequest.tenant_id == self.tenant_id,
            AppointmentRequest.customer_id == self.customer_id,
            AppointmentRequest.status != STATUS_CANCELLED,
        ))).scalar_one_or_none()

    async def _tool_get_my_appointments(self, tool_input: dict) -> dict:
        from datetime import datetime

        from sqlalchemy import select

        from app.models.appointment_request import APPOINTMENT_KINDS, STATUS_CONFIRMED, STATUS_REQUESTED, AppointmentRequest
        from app.services.local_time import as_utc, format_local

        tenant, _, zone, now = await self._booking_context()
        rows = (await self.db.execute(select(AppointmentRequest).where(
            AppointmentRequest.tenant_id == self.tenant_id,
            AppointmentRequest.customer_id == self.customer_id,
            AppointmentRequest.status.in_([STATUS_CONFIRMED, STATUS_REQUESTED]),
        ))).scalars().all()
        items = []
        for a in rows:
            if a.status == STATUS_CONFIRMED and (a.scheduled_at is None or as_utc(a.scheduled_at) < now):
                continue
            items.append({
                "appointment_id": str(a.id),
                "type": APPOINTMENT_KINDS.get(a.kind, a.kind),
                "vehicle": a.vehicle_label,
                "when": format_local(a.scheduled_at, zone) if a.scheduled_at else None,
                "status": "confirmé" if a.status == STATUS_CONFIRMED else "en attente de confirmation par un conseiller",
            })
        if not items:
            return {"appointments": [], "instruction": "Ce client n'a aucun rendez-vous à venir : dis-le simplement."}
        result = {"appointments": items}
        # Lot 35b — test réel du 30/09 : Bob ne proposait pas de nouveaux créneaux. Les créneaux libres
        # sont donnés d'office, pour qu'un déplacement se fasse sans étape oubliée.
        from app.services import booking

        settings = await booking.load_settings(self.db, self.tenant_id)
        if booking.booking_enabled(settings):
            slots = await booking.free_slots(self.db, tenant, settings, zone, now)
            if slots:
                result["available_slots"] = booking.slots_for_ai(slots, zone)
                result["instruction"] = (
                    "Si le client veut déplacer son rendez-vous, propose-lui TOUT DE SUITE ces créneaux libres, avec "
                    "leurs libellés exacts (ou appelle get_available_slots avec le jour qu'il souhaite). Quand il en "
                    "choisit un, appelle reschedule_my_appointment avec appointment_id et slot. S'il veut annuler, "
                    "appelle cancel_my_appointment."
                )
        return result

    async def _record_change(self, appointment, previous_when, text: str, note: str) -> None:
        self.message_outbox.append(text)
        self.change_outbox.append((appointment, previous_when))
        self.db.add(Message(
            tenant_id=self.tenant_id, conversation_id=self.conversation.id, sender=MessageSender.SYSTEM,
            message_type="appointment_changed", content=note,
        ))
        await self.db.flush()

    async def _confirms_proposed_cancellation(self) -> bool:
        """« Oui » en réponse à « Préférez-vous annuler ? » : une annulation explicite aussi."""
        from sqlalchemy import select

        if not _AFFIRMATIVE.match(self.incoming_text or ""):
            return False
        last_ai = (await self.db.execute(
            select(Message.content).where(Message.conversation_id == self.conversation.id, Message.sender == MessageSender.AI)
            .order_by(Message.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        return bool(last_ai and "annul" in last_ai.lower())

    async def _tool_cancel_my_appointment(self, tool_input: dict) -> dict:
        from app.services import appointment_service, booking

        appointment = await self._my_appointment(tool_input.get("appointment_id"))
        if appointment is None:
            return {"error": "Rendez-vous introuvable pour ce client : appelle get_my_appointments."}
        tenant, settings, zone, now = await self._booking_context()
        if self.incoming_text is not None and not asks_explicit_cancellation(self.incoming_text) \
                and not await self._confirms_proposed_cancellation():
            # Lot 35c — test réel du 30/09 : « Je ne veux plus venir lundi » a été annulé d'office. Sans
            # demande explicite d'annulation, on propose d'abord de déplacer.
            result = {"error": "Le client n'a pas demandé explicitement d'annuler : ne l'annule pas.",
                      "instruction": "Propose-lui d'abord de déplacer son rendez-vous (créneaux ci-dessous, libellés "
                                     "exacts), et demande-lui s'il préfère plutôt annuler."}
            if booking.booking_enabled(settings):
                result["available_slots"] = booking.slots_for_ai(
                    await booking.free_slots(self.db, tenant, settings, zone, now), zone)
            return result
        previous = appointment.scheduled_at
        appointment_service.cancel(appointment, now=now)
        appointment.cancelled_by = "CLIENT"
        await self._record_change(
            appointment, previous, appointment_service.cancellation_message(appointment, tenant.name, zone),
            f"Rendez-vous annulé par le client : {appointment_service.subject_phrase(appointment)}",
        )
        return {"status": "cancelled", "instruction": "L'annulation est faite et un message de confirmation part "
                "automatiquement juste après ta réponse : réponds brièvement, sans répéter la date."}

    async def _tool_reschedule_my_appointment(self, tool_input: dict) -> dict:
        from app.models.appointment_request import STATUS_CONFIRMED
        from app.services import appointment_service, booking
        from app.services.local_time import format_local

        appointment = await self._my_appointment(tool_input.get("appointment_id"))
        if appointment is None:
            return {"error": "Rendez-vous introuvable pour ce client : appelle get_my_appointments."}
        tenant, settings, zone, now = await self._booking_context(lock=True)
        if not booking.booking_enabled(settings):
            return {"error": "Pas de créneaux en ligne : transmets la demande à un conseiller avec handoff_to_human."}
        try:
            start = booking.parse_slot_id(tool_input.get("slot"), zone)
        except ValueError:
            start = None
        if start is None or not await booking.is_bookable(self.db, tenant, settings, zone, now, start, exclude_id=appointment.id):
            fresh = await booking.free_slots(self.db, tenant, settings, zone, now)
            return {"error": "Ce créneau n'est pas disponible (déjà pris, fermé ou passé).",
                    "available_slots": booking.slots_for_ai(fresh, zone),
                    "instruction": "Propose au client ces créneaux libres à la place."}
        previous = appointment.scheduled_at
        appointment.status = STATUS_CONFIRMED
        appointment.scheduled_at = start.astimezone(timezone.utc)
        appointment.confirmed_at = appointment.confirmed_at or now
        appointment.confirmed_by = appointment.confirmed_by or "BOB"
        appointment.availability = format_local(appointment.scheduled_at, zone)
        appointment.rescheduled_at, appointment.rescheduled_by = now, "CLIENT"
        # Nouvelle date : les rappels de l'ancienne ne comptent plus.
        appointment.reminder_sent_at = appointment.customer_reminder_sent_at = None
        appointment.customer_whatsapp_reminder_sent_at = None
        await self._record_change(
            appointment, previous, appointment_service.rescheduled_message(appointment, tenant.name, zone),
            f"Rendez-vous déplacé par le client : {appointment_service.subject_phrase(appointment)} — "
            f"{format_local(appointment.scheduled_at, zone)}",
        )
        return {"status": "rescheduled", "when": format_local(appointment.scheduled_at, zone),
                "instruction": "Le rendez-vous est déplacé et un message de confirmation part automatiquement juste "
                               "après ta réponse : réponds brièvement, sans changer la date."}

    async def _tool_negotiate_price(self, tool_input: dict) -> dict:
        from decimal import Decimal, InvalidOperation
        from sqlalchemy import select

        product_id = tool_input.get("product_id")
        try:
            offer = Decimal(str(tool_input.get("customer_offer")))
            product_uuid = uuid.UUID(product_id)
        except (InvalidOperation, ValueError, TypeError):
            return {"error": "Offre ou identifiant produit invalide"}

        settings_stmt = select(TenantNegotiationSettings).where(TenantNegotiationSettings.tenant_id == self.tenant_id)
        settings = (await self.db.execute(settings_stmt)).scalar_one_or_none()
        if settings is None or not settings.enabled:
            return {"error": "La négociation n'est pas activée pour cette entreprise"}

        from app.models.tenant import Tenant

        tenant = await self.db.get(Tenant, self.tenant_id)
        if tenant is None or not tenant.is_paid:
            return {"error": "La négociation nécessite un plan payant"}

        try:
            result = await negotiate_price(
                self.db, self.tenant_id, self.conversation, product_uuid, offer, settings
            )
        except NegotiationError as exc:
            return {"error": exc.message}

        return result

    async def _tool_record_marketing_consent(self, tool_input: dict) -> dict:
        from app.models.customer import Customer
        from app.services.consent_service import WITHDRAWN_VIA_AI, grant_marketing_consent, withdraw_marketing_consent

        customer = await self.db.get(Customer, self.customer_id)
        if customer is None:
            return {"error": "Client introuvable"}

        if tool_input.get("accepted"):
            grant_marketing_consent(customer, source="AI_ASKED")
        else:
            withdraw_marketing_consent(customer, source=WITHDRAWN_VIA_AI)

        await self.db.flush()
        return {"status": "saved"}

    async def _tool_record_customer_email(self, tool_input: dict) -> dict:
        """Lot 34 : adresse vérifiée par le code avant d'être enregistrée."""
        from app.models.customer import Customer
        from app.services.contact_capture import SOURCE_BOB, record_email, valid_email

        customer = await self.db.get(Customer, self.customer_id)
        if customer is None:
            return {"error": "Client introuvable"}
        email = valid_email(tool_input.get("email"))
        if email is None:
            return {"error": "Adresse email invalide : demande poliment au client de la vérifier."}
        record_email(customer, email, SOURCE_BOB)
        await self.db.flush()
        return {"status": "saved", "instruction": "Remercie simplement le client, sans répéter son adresse."}

    async def _email_ask(self, reason: str) -> str | None:
        """Lot 34b : la demande d'email part en message fixe ; Bob est seulement prévenu."""
        from app.models.customer import Customer
        from app.models.tenant import Tenant
        from app.services.address_form import uses_tu
        from app.services.contact_capture import NOTE_FOR_BOB, request_email

        text = request_email(await self.db.get(Customer, self.customer_id), reason,
                             tu=uses_tu(await self.db.get(Tenant, self.tenant_id)))
        if text is None:
            return None
        self.message_outbox.append(text)
        return NOTE_FOR_BOB

    async def _tool_share_payment_link(self, tool_input: dict) -> dict:
        from app.models.tenant import Tenant

        tenant = await self.db.get(Tenant, self.tenant_id)
        if tenant is None or not tenant.payment_link:
            return {"error": "Aucun lien de paiement configuré par cette entreprise"}
        return {"payment_link": tenant.payment_link}

    async def _tool_update_prospect_profile(self, tool_input: dict) -> dict:
        """Lot 43 (concession) : fiche prospect remplie au fil de la conversation."""
        from app.services import prospect

        fields = {k: tool_input.get(k) for k in ("need", "condition", "budget", "payment", "timeline", "trade_in")}
        if all(v is None for v in fields.values()):
            return {"error": "Aucune information à enregistrer."}
        _, rejected = await prospect.update_profile(self.db, self.tenant_id, self.customer_id, fields)
        if rejected:
            return {"status": "partially_saved", "rejected": rejected,
                    "instruction": "Ces valeurs n'ont pas été comprises : n'insiste pas, continue la conversation."}
        return {"status": "saved", "instruction": "Continue naturellement ; ne récite pas la fiche au client."}

    async def _tool_update_customer_profile(self, tool_input: dict) -> dict:
        from app.models.customer import Customer

        customer = await self.db.get(Customer, self.customer_id)
        if customer is None:
            return {"error": "Client introuvable"}

        if tool_input.get("first_name"):
            customer.first_name = tool_input["first_name"]
        if tool_input.get("city"):
            customer.city = tool_input["city"]

        preferences = dict(customer.detected_preferences or {})
        if tool_input.get("need"):
            preferences["need"] = tool_input["need"]
        if tool_input.get("brand"):
            preferences["brand"] = tool_input["brand"]
        if tool_input.get("budget_max") is not None:
            preferences["budget_max"] = tool_input["budget_max"]
        customer.detected_preferences = preferences

        await self.db.flush()
        return {"status": "saved"}

    async def _tool_check_order_status(self, tool_input: dict) -> dict:
        from sqlalchemy import select

        order_id = tool_input.get("order_id")
        if order_id:
            try:
                order_uuid = uuid.UUID(order_id)
            except (ValueError, TypeError):
                return {"error": "Identifiant de commande invalide"}
            stmt = select(Order).where(
                Order.tenant_id == self.tenant_id, Order.id == order_uuid, Order.customer_id == self.customer_id
            )
        else:
            stmt = (
                select(Order)
                .where(Order.tenant_id == self.tenant_id, Order.customer_id == self.customer_id)
                .order_by(Order.created_at.desc())
                .limit(1)
            )
        order = (await self.db.execute(stmt)).scalar_one_or_none()
        if order is None:
            return {"error": "Aucune commande trouvée pour ce client"}

        items_stmt = select(OrderItem).where(OrderItem.order_id == order.id)
        items = (await self.db.execute(items_stmt)).scalars().all()
        item_dicts = []
        for item in items:
            product = await self.product_repo.get(tenant_id=self.tenant_id, record_id=item.product_id)
            item_dicts.append({"name": product.name if product else "Produit supprimé", "quantity": item.quantity})

        delivery_stmt = select(Delivery).where(Delivery.tenant_id == self.tenant_id, Delivery.order_id == order.id)
        delivery = (await self.db.execute(delivery_stmt)).scalar_one_or_none()

        return {
            "order_id": str(order.id),
            "order_status": order.status.value,
            "total_amount": float(order.total_amount),
            "currency": order.currency,
            "items": item_dicts,
            "delivery_status": delivery.status.value if delivery else None,
            "tracking_number": delivery.tracking_number if delivery else None,
        }

    async def _tool_create_order(self, tool_input: dict) -> dict:
        try:
            order = await create_order(
                db=self.db,
                tenant_id=self.tenant_id,
                customer_id=self.customer_id,
                items=tool_input.get("items", []),
                delivery_address=tool_input.get("delivery_address"),
                payment_method=tool_input.get("payment_method"),
                created_by="IA",
                conversation_id=self.conversation.id,
            )
        except OrderCreationError as exc:
            return {"error": exc.message}

        await self._send_order_confirmation(order)

        from app.services.contact_capture import REASON_ORDER

        result = {
            "order_id": str(order.id),
            "status": order.status.value,
            "total_amount": float(order.total_amount),
            "currency": order.currency,
        }
        ask = await self._email_ask(REASON_ORDER)  # lot 34
        if ask:
            result["instruction"] = ask
        return result

    async def _send_order_confirmation(self, order) -> None:
        """
        Section 13/39 — message déterministe envoyé UNE SEULE FOIS à la création de la
        commande. Ce n'est PAS un reçu de paiement (aucun paiement n'est encore confirmé
        à ce stade) — juste une confirmation + le lien de paiement si configuré. Un échec
        d'envoi ne doit jamais faire échouer la commande elle-même.
        """
        from sqlalchemy import select

        from app.models.customer import Customer
        from app.models.tenant import Tenant
        from app.models.whatsapp_account import WhatsAppAccount
        from app.services.receipt_service import generate_order_confirmation_text, get_order_item_lines

        try:
            item_lines = await get_order_item_lines(self.db, order.id, order.currency)
            tenant = await self.db.get(Tenant, self.tenant_id)
            from app.services.address_form import uses_tu

            confirmation_text = generate_order_confirmation_text(
                order, item_lines, tenant.name if tenant else "", tenant.payment_link if tenant else None,
                tu=uses_tu(tenant),
            )

            self.db.add(
                Message(
                    tenant_id=self.tenant_id, conversation_id=self.conversation.id,
                    sender=MessageSender.SYSTEM, message_type="order_confirmation", content=confirmation_text,
                )
            )
            await self.db.flush()

            account_stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == self.tenant_id)
            account = (await self.db.execute(account_stmt)).scalar_one_or_none()
            customer = await self.db.get(Customer, self.customer_id)
            if account is not None and customer is not None:
                from app.integrations.whatsapp.client import WhatsAppClient

                wa_client = WhatsAppClient(phone_number_id=account.phone_number_id, system_user_token=account.system_user_token)
                await wa_client.send_text_message(to=customer.whatsapp_number, body=confirmation_text)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("Échec de l'envoi de la confirmation pour la commande %s", order.id)


def tool_result_to_text(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)
