"""Lot 37 — « Rester connecté sur cet appareil » (15 jours) et application installable (PWA)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.audit_log import AuditLog
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.user_session import UserSession
from app.services import sessions
from app.services.otp_service import hash_otp

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
REVOKE_ALL = "/api/v1/auth/sessions/revoke-all"
DASHBOARD = Path(__file__).resolve().parents[1] / "static" / "dashboard"


async def _user(db_session, email, mfa_enabled=False) -> User:
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    user = User(tenant_id=tenant.id, email=email, hashed_password=hash_password("motdepasse1"), full_name="Awa",
                role=Role.OWNER, mfa_enabled=mfa_enabled)
    db_session.add(user)
    await db_session.commit()
    return user


def _cookie(response) -> str | None:
    """Clé de session posée par la réponse (None si aucune ; '' si le cookie est effacé)."""
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{sessions.COOKIE_NAME}="):
            value = header.split(";", 1)[0].split("=", 1)[1]
            return value.strip('"')
    return None


def _set_cookie_header(response) -> str:
    return next(h for h in response.headers.get_list("set-cookie") if h.startswith(f"{sessions.COOKIE_NAME}="))


async def _post(client, path, raw=None, **kwargs):
    client.cookies.clear()  # on maîtrise exactement la clé envoyée
    headers = kwargs.pop("headers", {})
    if raw is not None:
        headers["Cookie"] = f"{sessions.COOKIE_NAME}={raw}"
    return await client.post(path, headers=headers, **kwargs)


async def _login(client, email, remember=True):
    data = {"username": email, "password": "motdepasse1"}
    if remember:
        data["remember"] = "true"
    return await _post(client, LOGIN, data=data)


async def _sessions_of(db_session, user_id) -> list[UserSession]:
    return (await db_session.execute(
        select(UserSession).where(UserSession.user_id == user_id).execution_options(populate_existing=True)
    )).scalars().all()


# --- Connexion ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_with_remember_sets_protected_cookie(client, db_session, unique_email):
    user = await _user(db_session, unique_email)
    r = await _login(client, unique_email)
    assert r.status_code == 200 and r.json()["access_token"]
    raw = _cookie(r)
    assert raw and len(raw) >= 40
    header = _set_cookie_header(r).lower()
    assert "httponly" in header and "samesite=strict" in header and "path=/" in header
    assert "max-age=1296000" in header  # 15 jours
    assert "secure" in header  # adresse publique en https
    stored = await _sessions_of(db_session, user.id)
    assert len(stored) == 1
    assert stored[0].token_hash != raw and stored[0].token_hash == sessions._hash(raw)  # seule l'empreinte en base
    assert sessions._aware(stored[0].expires_at) > datetime.now(timezone.utc) + timedelta(days=14, hours=23)


@pytest.mark.asyncio
async def test_login_without_remember_sets_no_cookie(client, db_session, unique_email):
    user = await _user(db_session, unique_email)
    r = await _login(client, unique_email, remember=False)
    assert r.status_code == 200
    assert _cookie(r) is None
    assert await _sessions_of(db_session, user.id) == []


@pytest.mark.asyncio
async def test_wrong_password_with_remember_sets_no_cookie(client, db_session, unique_email):
    user = await _user(db_session, unique_email)
    r = await _post(client, LOGIN, data={"username": unique_email, "password": "faux", "remember": "true"})
    assert r.status_code == 401 and _cookie(r) is None
    assert await _sessions_of(db_session, user.id) == []


@pytest.mark.asyncio
async def test_mfa_session_only_after_code(client, db_session, unique_email):
    user = await _user(db_session, unique_email, mfa_enabled=True)
    r = await _login(client, unique_email)
    assert r.json()["mfa_required"] is True
    assert _cookie(r) is None  # mot de passe seul : jamais de session longue
    assert await _sessions_of(db_session, user.id) == []

    await db_session.refresh(user)
    user.mfa_otp_code_hash = hash_otp("123456")
    await db_session.commit()
    bad = await _post(client, "/api/v1/auth/verify-mfa",
                      json={"mfa_pending_token": r.json()["mfa_pending_token"], "code": "000000", "remember": True})
    assert bad.status_code == 401 and _cookie(bad) is None
    ok = await _post(client, "/api/v1/auth/verify-mfa",
                     json={"mfa_pending_token": r.json()["mfa_pending_token"], "code": "123456", "remember": True})
    assert ok.status_code == 200 and _cookie(ok)
    assert len(await _sessions_of(db_session, user.id)) == 1


@pytest.mark.asyncio
async def test_mfa_without_remember_sets_no_cookie(client, db_session, unique_email):
    user = await _user(db_session, unique_email, mfa_enabled=True)
    r = await _login(client, unique_email, remember=False)
    await db_session.refresh(user)
    user.mfa_otp_code_hash = hash_otp("123456")
    await db_session.commit()
    ok = await _post(client, "/api/v1/auth/verify-mfa",
                     json={"mfa_pending_token": r.json()["mfa_pending_token"], "code": "123456"})
    assert ok.status_code == 200 and _cookie(ok) is None


# --- Renouvellement ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_gives_access_and_rotates_key(client, db_session, unique_email):
    user = await _user(db_session, unique_email)
    raw = _cookie(await _login(client, unique_email))
    r = await _post(client, REFRESH, raw)
    assert r.status_code == 200
    token = r.json()["access_token"]
    new_raw = _cookie(r)
    assert new_raw and new_raw != raw
    me = await client.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["id"] == str(user.tenant_id)
    # la nouvelle clé fonctionne à son tour
    assert (await _post(client, REFRESH, new_raw)).status_code == 200


@pytest.mark.asyncio
async def test_refresh_without_or_with_unknown_cookie_is_refused(client, db_session, unique_email):
    await _user(db_session, unique_email)
    r = await _post(client, REFRESH)
    assert r.status_code == 401
    r = await _post(client, REFRESH, "inconnue-" + "x" * 40)
    assert r.status_code == 401
    assert _cookie(r) == ""  # le cookie est effacé
    assert (await _post(client, REFRESH, "x" * 500)).status_code == 401


@pytest.mark.asyncio
async def test_old_key_accepted_briefly_then_refused(db_session, unique_email):
    user = await _user(db_session, unique_email)
    now = datetime.now(timezone.utc)
    raw = await sessions.create_session(db_session, user, "Test", now)
    await db_session.commit()
    session, new_raw = await sessions.use_session(db_session, raw, now)
    assert session is not None and new_raw
    await db_session.commit()
    # deux onglets en même temps : l'ancienne clé passe encore, sans nouvelle rotation
    again, none = await sessions.use_session(db_session, raw, now + timedelta(seconds=30))
    assert again is not None and none is None
    late, _ = await sessions.use_session(db_session, raw, now + timedelta(seconds=61))
    assert late is None
    # la clé actuelle, elle, reste bonne
    current, _ = await sessions.use_session(db_session, new_raw, now + timedelta(seconds=61))
    assert current is not None


@pytest.mark.asyncio
async def test_old_key_does_not_extend_session(db_session, unique_email):
    user = await _user(db_session, unique_email)
    now = datetime.now(timezone.utc)
    raw = await sessions.create_session(db_session, user, None, now)
    session, _ = await sessions.use_session(db_session, raw, now)
    expiry = session.expires_at
    await sessions.use_session(db_session, raw, now + timedelta(seconds=30))
    assert session.expires_at == expiry


@pytest.mark.asyncio
async def test_session_expires_after_15_days_without_use(db_session, unique_email):
    user = await _user(db_session, unique_email)
    now = datetime.now(timezone.utc)
    raw = await sessions.create_session(db_session, user, None, now)
    await db_session.commit()
    expired, _ = await sessions.use_session(db_session, raw, now + timedelta(days=15, seconds=1))
    assert expired is None


@pytest.mark.asyncio
async def test_session_is_sliding(db_session, unique_email):
    """Utilisée au 14e jour, elle repart pour 15 jours."""
    user = await _user(db_session, unique_email)
    now = datetime.now(timezone.utc)
    raw = await sessions.create_session(db_session, user, None, now)
    session, new_raw = await sessions.use_session(db_session, raw, now + timedelta(days=14))
    assert session is not None
    later, _ = await sessions.use_session(db_session, new_raw, now + timedelta(days=28))
    assert later is not None


@pytest.mark.asyncio
async def test_refresh_refused_for_suspended_user_and_session_revoked(client, db_session, unique_email):
    user = await _user(db_session, unique_email)
    raw = _cookie(await _login(client, unique_email))
    user.active = False
    await db_session.commit()
    r = await _post(client, REFRESH, raw)
    assert r.status_code == 401 and _cookie(r) == ""
    stored = await _sessions_of(db_session, user.id)
    assert stored[0].revoked_at is not None
    user.active = True
    await db_session.commit()
    assert (await _post(client, REFRESH, raw)).status_code == 401  # réactivé : il faut se reconnecter


@pytest.mark.asyncio
async def test_refresh_refused_for_suspended_tenant(client, db_session, unique_email):
    user = await _user(db_session, unique_email)
    raw = _cookie(await _login(client, unique_email))
    tenant = await db_session.get(Tenant, user.tenant_id)
    tenant.active = False
    await db_session.commit()
    assert (await _post(client, REFRESH, raw)).status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_belongs_to_session_owner(client, db_session, unique_email):
    """Deux boutiques : chaque clé ne donne accès qu'à sa propre boutique."""
    a = await _user(db_session, unique_email)
    b = await _user(db_session, "b_" + unique_email)
    raw_a = _cookie(await _login(client, unique_email))
    raw_b = _cookie(await _login(client, "b_" + unique_email))
    for raw, user in ((raw_a, a), (raw_b, b)):
        token = (await _post(client, REFRESH, raw)).json()["access_token"]
        me = await client.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["id"] == str(user.tenant_id)


# --- Déconnexion -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logout_revokes_this_device_only(client, db_session, unique_email):
    user = await _user(db_session, unique_email)
    phone = _cookie(await _login(client, unique_email))
    laptop = _cookie(await _login(client, unique_email))
    r = await _post(client, LOGOUT, phone)
    assert r.status_code == 204 and _cookie(r) == ""
    assert (await _post(client, REFRESH, phone)).status_code == 401
    assert (await _post(client, REFRESH, laptop)).status_code == 200
    assert len(await _sessions_of(db_session, user.id)) == 2


@pytest.mark.asyncio
async def test_logout_without_cookie_is_harmless(client, db_session, unique_email):
    await _user(db_session, unique_email)
    assert (await _post(client, LOGOUT)).status_code == 204


@pytest.mark.asyncio
async def test_revoke_all_disconnects_every_device(client, db_session, unique_email):
    user = await _user(db_session, unique_email)
    other = await _user(db_session, "autre_" + unique_email)
    phone = _cookie(await _login(client, unique_email))
    laptop = _cookie(await _login(client, unique_email))
    foreign = _cookie(await _login(client, "autre_" + unique_email))
    token = (await _post(client, REFRESH, laptop)).json()["access_token"]

    assert (await _post(client, REVOKE_ALL)).status_code == 401  # connexion obligatoire
    r = await _post(client, REVOKE_ALL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 204 and _cookie(r) == ""
    assert (await _post(client, REFRESH, phone)).status_code == 401
    assert all(s.revoked_at is not None for s in await _sessions_of(db_session, user.id))
    assert (await _post(client, REFRESH, foreign)).status_code == 200  # l'autre boutique n'est pas touchée
    assert all(s.revoked_at is None for s in await _sessions_of(db_session, other.id))
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "SESSIONS_REVOKED_ALL"))).scalars().all()
    assert len(audit) == 1


@pytest.mark.asyncio
async def test_password_reset_disconnects_every_device(client, db_session, unique_email, monkeypatch):
    sent = []
    monkeypatch.setattr("app.api.auth.routes.send_email", lambda to, subject, body: sent.append(body) or True)
    user = await _user(db_session, unique_email)
    raw = _cookie(await _login(client, unique_email))
    await _post(client, "/api/v1/auth/forgot-password", json={"email": unique_email})
    import re

    token = re.search(r"reset_token=([A-Za-z0-9_\-]+)", sent[-1]).group(1)
    r = await _post(client, "/api/v1/auth/reset-password", json={"token": token, "new_password": "nouveau-mdp-123"})
    assert r.status_code == 204
    assert (await _post(client, REFRESH, raw)).status_code == 401
    assert all(s.revoked_at is not None for s in await _sessions_of(db_session, user.id))


@pytest.mark.asyncio
async def test_refresh_is_rate_limited(client, db_session, unique_email):
    await _user(db_session, unique_email)
    codes = [(await _post(client, REFRESH, "x" * 43)).status_code for _ in range(61)]
    assert codes[:60] == [401] * 60 and codes[60] == 429


# --- Application installable (PWA) ---------------------------------------------------------

def test_manifest_is_valid_and_installable():
    manifest = json.loads((DASHBOARD / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] and manifest["name"] and manifest["short_name"]
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any("maskable" in icon.get("purpose", "") for icon in manifest["icons"])
    for icon in manifest["icons"]:
        assert (DASHBOARD / icon["src"].lstrip("./")).exists(), icon["src"]


def test_icons_have_announced_sizes():
    from PIL import Image

    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("icon-maskable-512.png", 512),
                       ("apple-touch-icon.png", 180)):
        with Image.open(DASHBOARD / name) as image:
            assert image.size == (size, size), name


def test_service_worker_never_caches_api():
    sw = (DASHBOARD / "sw.js").read_text(encoding="utf-8")
    assert 'mode !== "navigate") return' in sw  # seules les ouvertures de page ; l'API passe toujours par le réseau
    assert "cache.put" not in sw and "addAll" not in sw  # rien d'autre n'est gardé sur l'appareil
    assert "offline.html" in sw
    assert (DASHBOARD / "offline.html").exists()


def test_dashboard_declares_pwa_and_remember_checkbox():
    html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' in html and "manifest.webmanifest" in html
    assert 'name="theme-color"' in html and "apple-touch-icon" in html
    assert 'id="login-remember"' in html and "15 jours" in html
    assert "serviceWorker" in html and "/api/v1/auth/refresh" in html
    assert "/api/v1/auth/logout" in html and "/api/v1/auth/sessions/revoke-all" in html


@pytest.mark.asyncio
async def test_pwa_files_are_served(client):
    for path in ("manifest.webmanifest", "sw.js", "offline.html", "icon-192.png"):
        r = await client.get(f"/dashboard/{path}")
        assert r.status_code == 200, path
