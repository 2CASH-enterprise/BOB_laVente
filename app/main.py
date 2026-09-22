from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.analytics.routes import router as analytics_router
from app.api.auth.routes import router as auth_router
from app.api.catalog.categories import router as categories_router
from app.api.catalog.complements import router as complements_router
from app.api.catalog.import_ import router as catalog_import_router
from app.api.catalog.products import router as products_router
from app.api.conversations.routes import router as conversations_router
from app.api.demo.routes import router as demo_router
from app.api.integrations.meta_catalog import router as meta_catalog_router
from app.api.integrations.shopify import router as shopify_router
from app.api.knowledge.routes import router as knowledge_router
from app.api.qrcodes.routes import router as qrcodes_router
from app.api.messages.routes import router as messages_router
from app.api.orders.routes import router as orders_router
from app.api.tenants.routes import router as tenants_router
from app.api.webhooks.whatsapp import router as whatsapp_webhook_router
from app.api.whatsapp.routes import router as whatsapp_router
from app.core.config import get_settings
from app.core.security_headers import SecurityHeadersMiddleware

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Plateforme SaaS multi-tenant — agent vendeur IA connecté à WhatsApp",
)

app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth_router)
app.include_router(tenants_router)
app.include_router(whatsapp_webhook_router)
app.include_router(whatsapp_router)
app.include_router(messages_router)
app.include_router(products_router)
app.include_router(categories_router)
app.include_router(complements_router)
app.include_router(catalog_import_router)
app.include_router(orders_router)
app.include_router(conversations_router)
app.include_router(analytics_router)
app.include_router(shopify_router)
app.include_router(meta_catalog_router)
app.include_router(knowledge_router)
app.include_router(demo_router)
app.include_router(qrcodes_router)

_static_dir = Path(__file__).parent / "static" / "dashboard"
if _static_dir.exists():
    app.mount("/dashboard", StaticFiles(directory=str(_static_dir), html=True), name="dashboard")

_legal_dir = Path(__file__).parent / "static" / "legal"
if _legal_dir.exists():
    app.mount("/legal", StaticFiles(directory=str(_legal_dir), html=True), name="legal")

_demo_dir = Path(__file__).parent / "static" / "instant-demo"
if _demo_dir.exists():
    app.mount("/demo", StaticFiles(directory=str(_demo_dir), html=True), name="instant-demo")


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Montée en tout dernier : un mount à "/" intercepterait sinon toutes les routes
# déclarées après lui (API, /dashboard, /demo, /legal, /health).
_landing_dir = Path(__file__).parent / "static" / "landing"
if _landing_dir.exists():
    app.mount("/", StaticFiles(directory=str(_landing_dir), html=True), name="landing")
