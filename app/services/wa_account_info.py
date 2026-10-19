"""
Lot 73 d — le numéro et le nom d'un compte WhatsApp viennent de Meta : numéro affiché, nom vérifié, statut du nom.
Demandés au branchement, quand la page Intégrations s'ouvre (au plus toutes les 5 minutes) et chaque nuit.
Une erreur de Meta ne casse jamais rien : l'état connu reste affiché.
"""
import logging

from sqlalchemy import select

from app.models.whatsapp_account import DisplayNameStatus, WhatsAppAccount

logger = logging.getLogger(__name__)

# name_status de Meta → statut affiché par Bob
NAME_STATUS = {
    "APPROVED": DisplayNameStatus.APPROVED, "AVAILABLE_WITHOUT_REVIEW": DisplayNameStatus.APPROVED,
    "DECLINED": DisplayNameStatus.REJECTED, "EXPIRED": DisplayNameStatus.REJECTED,
}


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


async def refresh(db, account: WhatsAppAccount, oauth_client) -> bool:
    """Met à jour le compte d'après Meta. Renvoie True si Meta a répondu."""
    try:
        info = await oauth_client.phone_number_info(account.phone_number_id, account.system_user_token)
    except Exception as exc:  # noqa: BLE001 — Meta injoignable ou jeton expiré : on garde l'état connu
        logger.warning("Infos du numéro WhatsApp indisponibles (boutique %s) : %s", account.tenant_id, type(exc).__name__)
        return False
    digits = _digits(info.get("display_phone_number"))
    if digits and not account.display_phone_number:  # un numéro saisi à la main n'est jamais écrasé
        account.display_phone_number = "+" + digits
    if info.get("verified_name"):
        account.display_name = str(info["verified_name"])[:255]
    status = str(info.get("name_status") or "").upper()
    if status:
        account.display_name_status = NAME_STATUS.get(status, DisplayNameStatus.PENDING)
    await db.commit()
    return True


async def refresh_all(db, oauth_client=None) -> dict:
    if oauth_client is None:
        from app.integrations.whatsapp.dependency import get_meta_oauth_client

        oauth_client = get_meta_oauth_client()
    accounts = (await db.execute(select(WhatsAppAccount))).scalars().all()
    done = 0
    for account in accounts:
        done += await refresh(db, account, oauth_client)
    return {"accounts": len(accounts), "refreshed": done}
