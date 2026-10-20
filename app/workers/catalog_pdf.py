"""Lot 88 — lecture par l'IA d'un catalogue PDF mis en page (brochure, scan), en arrière-plan."""
import asyncio

from app.workers.celery_app import celery_app
from app.workers.inbound_leads import _with_db


@celery_app.task(name="app.workers.catalog_pdf.read_catalog_pdf_task")
def read_catalog_pdf_task(import_id: str) -> str:
    from uuid import UUID

    from app.services.catalog_pdf import process_ai_import

    return asyncio.run(_with_db(lambda db: process_ai_import(db, UUID(import_id))))
