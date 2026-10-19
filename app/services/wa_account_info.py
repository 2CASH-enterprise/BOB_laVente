"""
Lot 73 d — le numéro et le nom d'un compte WhatsApp viennent de Meta : numéro affiché, nom vérifié, statut du nom.
Demandés au branchement, quand la page Intégrations s'ouvre (au plus toutes les 5 minutes) et chaque nuit.
Lot 73 e : la photo de profil aussi, enregistrée chez Bob (le lien de Meta expire) : uploads/wa-profiles/<boutique>.<ext>.
Une erreur de Meta ne casse jamais rien : l'état connu reste affiché.
"""
import hashlib
import logging
from pathlib import Path

from sqlalchemy import select

from app.models.whatsapp_account import DisplayNameStatus, WhatsAppAccount

logger = logging.getLogger(__name__)

# name_status de Meta → statut affiché par Bob
NAME_STATUS = {
    "APPROVED": DisplayNameStatus.APPROVED, "AVAILABLE_WITHOUT_REVIEW": DisplayNameStatus.APPROVED,
    "DECLINED": DisplayNameStatus.REJECTED, "EXPIRED": DisplayNameStatus.REJECTED,
}


PROFILES = Path(__file__).resolve().parents[1] / "static" / "uploads" / "wa-profiles"
PICTURE_MAX_BYTES = 2 * 1024 * 1024
PICTURE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _pictures(tenant_id, folder: Path | None = None) -> list[Path]:
    folder = folder or PROFILES
    return [folder / f"{tenant_id}.{ext}" for ext in PICTURE_TYPES.values() if (folder / f"{tenant_id}.{ext}").is_file()]


def profile_url(tenant_id, folder: Path | None = None) -> str | None:
    """Adresse de la photo enregistrée (avec ?v= pour que le navigateur prenne la nouvelle après un changement)."""
    found = _pictures(tenant_id, folder)
    if not found:
        return None
    return f"/uploads/wa-profiles/{found[0].name}?v={int(found[0].stat().st_mtime)}"


async def refresh_picture(account, oauth_client, folder: Path | None = None) -> str:
    """Met la photo de profil à jour. Renvoie « saved », « same », « removed », « none » ou « error »."""
    folder = folder or PROFILES
    try:
        url = await oauth_client.profile_picture_url(account.phone_number_id, account.system_user_token)
        if not url:  # plus de photo sur WhatsApp : on retire la nôtre
            removed = _pictures(account.tenant_id, folder)
            for old in removed:
                old.unlink()
            return "removed" if removed else "none"
        got = await oauth_client.download_image(url, PICTURE_MAX_BYTES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Photo de profil WhatsApp indisponible (boutique %s) : %s", account.tenant_id, type(exc).__name__)
        return "error"
    if not got or got[1] not in PICTURE_TYPES or not got[0]:
        return "error"
    content, kind = got
    target = folder / f"{account.tenant_id}.{PICTURE_TYPES[kind]}"
    if target.is_file() and hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(content).digest():
        return "same"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for old in _pictures(account.tenant_id, folder):
            if old != target:
                old.unlink()
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(content)
        tmp.replace(target)
    except OSError as exc:
        logger.warning("Photo de profil WhatsApp non enregistrée (boutique %s) : %s", account.tenant_id, exc)
        return "error"
    return "saved"


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
    await refresh_picture(account, oauth_client)
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
