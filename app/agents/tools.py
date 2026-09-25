"""
Exécution réelle des outils (section 33 : protection contre les hallucinations).

Principe absolu : cette classe est le SEUL endroit où prix, stock et existence d'un
produit sont déterminés. Le LLM ne fait jamais que lire ce qui est renvoyé ici — il
n'a aucun autre moyen d'obtenir ces informations (section 50 : le LLM comprend,
raisonne, utilise les outils, communique ; il n'est jamais la source de vérité).
"""
import json
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


class ToolExecutor:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID, conversation: Conversation, customer_id: uuid.UUID | None = None):
        self.db = db
        self.tenant_id = tenant_id
        self.conversation = conversation
        self.customer_id = customer_id or conversation.customer_id
        self.product_repo = ProductRepository(db)
        self.complement_repo = ProductComplementRepository(db)
        self.handoff_requested: bool = False
        self.handoff_reason: str | None = None

    async def execute(self, tool_name: str, tool_input: dict) -> dict:
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return {"error": f"Outil inconnu : {tool_name}"}
        return await handler(tool_input)

    async def _product_to_dict(self, product) -> dict:
        return {
            "product_id": str(product.id),
            "name": product.name,
            "price": float(product.price),
            "currency": product.currency,
            "stock": product.stock_quantity,
            "active": product.active,
        }

    async def _tool_search_products(self, tool_input: dict) -> dict:
        products = await self.product_repo.search(
            tenant_id=self.tenant_id,
            query=tool_input.get("query"),
            min_price=tool_input.get("min_price"),
            max_price=tool_input.get("max_price"),
            limit=10,
        )
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
        reason = tool_input.get("reason", "Non précisé")
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

    async def _tool_share_payment_link(self, tool_input: dict) -> dict:
        from app.models.tenant import Tenant

        tenant = await self.db.get(Tenant, self.tenant_id)
        if tenant is None or not tenant.payment_link:
            return {"error": "Aucun lien de paiement configuré par cette entreprise"}
        return {"payment_link": tenant.payment_link}

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

        return {
            "order_id": str(order.id),
            "status": order.status.value,
            "total_amount": float(order.total_amount),
            "currency": order.currency,
        }

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
            confirmation_text = generate_order_confirmation_text(
                order, item_lines, tenant.name if tenant else "", tenant.payment_link if tenant else None
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
