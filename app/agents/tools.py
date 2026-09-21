"""
Exécution réelle des outils (section 33 : protection contre les hallucinations).

Principe absolu : cette classe est le SEUL endroit où prix, stock et existence d'un
produit sont déterminés. Le LLM ne fait jamais que lire ce qui est renvoyé ici — il
n'a aucun autre moyen d'obtenir ces informations (section 50 : le LLM comprend,
raisonne, utilise les outils, communique ; il n'est jamais la source de vérité).
"""
import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.negotiation_settings import TenantNegotiationSettings
from app.repositories.product_complement_repository import ProductComplementRepository
from app.repositories.product_repository import ProductRepository
from app.services.negotiation_service import NegotiationError, negotiate_price
from app.services.order_service import OrderCreationError, create_order


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

        # Priorité aux produits en stock, puis les plus proches du budget (section 17, 22 : max 3 propositions)
        in_stock = [p for p in products if p.stock_quantity > 0]
        pool = in_stock or products
        if budget:
            pool = sorted(pool, key=lambda p: abs(float(p.price) - float(budget)))
        top3 = pool[:3]

        if not top3:
            return {"results": [], "message": "Aucun produit ne correspond à ce besoin dans le catalogue."}
        return {"results": [await self._product_to_dict(p) for p in top3]}

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

        try:
            result = await negotiate_price(
                self.db, self.tenant_id, self.conversation, product_uuid, offer, settings
            )
        except NegotiationError as exc:
            return {"error": exc.message}

        return result

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

        return {
            "order_id": str(order.id),
            "status": order.status.value,
            "total_amount": float(order.total_amount),
            "currency": order.currency,
        }


def tool_result_to_text(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)
