"""
Lot 73 e — photo de profil WhatsApp Business : récupérée chez Meta avec le nom, enregistrée chez Bob (le lien de Meta
expire), affichée dans la carte WhatsApp et dans la barre latérale à la place de l'initiale.
"""
import asyncio
from pathlib import Path

import pytest

from app.core.security import hash_password
from app.integrations.whatsapp.client import MetaOAuthClient
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import wa_account_info
from app.tests.test_lot73d_numero_meta import FakeInfo

DASH = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
JPEG = b"\xff\xd8\xff\xe0" + b"photo" * 10


class FakePicture(FakeInfo):
    def __init__(self, url="https://pps.whatsapp.net/v/t61/photo.jpg?oh=x", image=(JPEG, "image/jpeg"), **kw):
        super().__init__(**kw)
        self.url, self.image, self.downloads = url, image, 0

    async def profile_picture_url(self, phone_number_id, access_token):
        return self.url

    async def download_image(self, url, max_bytes):
        self.downloads += 1
        return self.image


async def _account(db, email):
    tenant = Tenant(name="MS4 Africa", country="CI", currency="XOF", email=email)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="Awa", role=Role.OWNER))
    account = WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="p", system_user_token="t")
    db.add(account)
    await db.commit()
    return account


@pytest.mark.asyncio
async def test_picture_saved_kept_replaced_and_removed(db_session, unique_email, tmp_path, monkeypatch):
    monkeypatch.setattr(wa_account_info, "PROFILES", tmp_path)
    account = await _account(db_session, unique_email)
    tid = account.tenant_id
    assert wa_account_info.profile_url(tid) is None

    assert await wa_account_info.refresh(db_session, account, FakePicture({"name_status": "APPROVED"})) is True
    assert (tmp_path / f"{tid}.jpg").read_bytes() == JPEG
    assert wa_account_info.profile_url(tid).startswith(f"/uploads/wa-profiles/{tid}.jpg?v=")

    assert await wa_account_info.refresh_picture(account, FakePicture()) == "same"
    png = (b"\x89PNG" + b"x" * 20, "image/png")
    assert await wa_account_info.refresh_picture(account, FakePicture(image=png)) == "saved"
    assert not (tmp_path / f"{tid}.jpg").exists() and (tmp_path / f"{tid}.png").exists()

    # Mauvais type, téléchargement refusé, Meta en panne : la photo connue reste
    assert await wa_account_info.refresh_picture(account, FakePicture(image=(b"<html>", "text/html"))) == "error"
    assert await wa_account_info.refresh_picture(account, FakePicture(image=None)) == "error"
    assert await wa_account_info.refresh_picture(account, Broken()) == "error"
    assert (tmp_path / f"{tid}.png").exists()

    # Plus de photo sur WhatsApp : retirée chez Bob aussi
    assert await wa_account_info.refresh_picture(account, FakePicture(url=None)) == "removed"
    assert wa_account_info.profile_url(tid) is None


class Broken(FakePicture):
    async def profile_picture_url(self, phone_number_id, access_token):
        raise RuntimeError("Meta injoignable")


def test_download_only_from_meta_servers():
    client = MetaOAuthClient("app", "secret")
    for bad in ["http://pps.whatsapp.net/a.jpg", "https://evil.com/a.jpg", "https://whatsapp.net.evil.com/a.jpg",
                "https://169.254.169.254/latest", "file:///etc/passwd", ""]:
        assert asyncio.run(client.download_image(bad, 1000)) is None, bad


@pytest.mark.asyncio
async def test_api_returns_picture_for_card_and_sidebar(client, db_session, unique_email, tmp_path, monkeypatch):
    monkeypatch.setattr(wa_account_info, "PROFILES", tmp_path)
    account = await _account(db_session, unique_email)
    (tmp_path / f"{account.tenant_id}.webp").write_bytes(b"RIFFxxxxWEBP")
    r = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    expected = f"/uploads/wa-profiles/{account.tenant_id}.webp?v="
    assert (await client.get("/api/v1/whatsapp/account", headers=headers)).json()["profile_picture_url"].startswith(expected)
    assert (await client.get("/api/v1/tenants/me", headers=headers)).json()["logo_url"].startswith(expected)


def test_dashboard_shows_the_picture():
    assert 'id="wa-photo"' in DASH and "function setTenantAvatar(url)" in DASH
    assert "if (t.logo_url) setTenantAvatar(t.logo_url);" in DASH
    assert "img.onerror" in DASH and "!account.profile_picture_url" in DASH


def test_picture_address_keeps_the_bob_prefix():
    """Lot 73 f — en production le tableau de bord est sous /bob : « /uploads/… » doit devenir « /bob/uploads/… »."""
    assert 'return path && path.startsWith("/") ? API_BASE + path : path;' in DASH
    assert "photo.src = siteUrl(account.profile_picture_url);" in DASH and "img.src = siteUrl(url);" in DASH
