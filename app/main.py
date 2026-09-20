from fastapi import FastAPI

from app.api.auth.routes import router as auth_router
from app.api.tenants.routes import router as tenants_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Plateforme SaaS multi-tenant — agent vendeur IA connecté à WhatsApp",
)

app.include_router(auth_router)
app.include_router(tenants_router)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
