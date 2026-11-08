"""Lot 116 — /appel/{jeton} : la page publique du bouton « 📞 Appeler » (compose le numéro de la boutique)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

router = APIRouter(tags=["calls"])


@router.get("/appel/{value}", response_class=HTMLResponse)
async def call_page(value: str, request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    from app.models.conversation import Conversation
    from app.models.tenant import Tenant
    from app.services import call_button, link_preview

    conversation_id = call_button.conversation_of(value)
    conversation = await db.get(Conversation, conversation_id) if conversation_id else None
    tenant = await db.get(Tenant, conversation.tenant_id) if conversation is not None else None
    number = await call_button.number_for(db, tenant) if tenant is not None else None
    if number is None:
        raise HTTPException(status_code=404, detail="Ce lien d'appel n'est plus valable")
    if not link_preview.is_crawler(request.headers.get("user-agent")) and await call_button.note_click(db, tenant.id, conversation.id):
        await db.commit()
    return HTMLResponse(call_button.page(tenant.name, number),
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})
