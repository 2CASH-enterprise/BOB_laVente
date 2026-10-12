"""
Lot 70 — corrections de l'audit de sécurité du 11/10/2026 (critique et élevées).

- C1 : aucun texte dans un onclick : arguments passés par jsArg (JSON puis échappement HTML), noms relus côté script.
- E1 : connexion Super Admin limitée (email et adresse), code à 6 chiffres par email, alerte à chaque connexion.
- E2 : uvicorn lit la vraie adresse du visiteur posée par nginx (et seulement par nginx).
- M1 : garde-fous de coût de la démo publique. M2 : messages WhatsApp non signés refusés.
- F1 : compte désactivé = accès coupé tout de suite. F4 : carte de l'API masquée hors développement.
"""
import hashlib
import hmac
import io
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.dependency import get_llm_client
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    create_mfa_pending_token,
    create_superadmin_access_token,
    create_superadmin_mfa_pending_token,
    hash_password,
)
from app.main import app
from app.models.conversation import Message
from app.models.superadmin_user import SuperAdminUser
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.tests.fakes import FakeLLMClient, text_response

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "static"
PAGES = {name: (STATIC / name / "index.html").read_text(encoding="utf-8") for name in ("dashboard", "superadmin", "instant-demo")}
LOGIN = "/api/v1/superadmin/login"
PASSWORD = "supersecret123"


# ------------------------------------------------------------------ C1 — onclick sans texte libre

def _handlers(html: str) -> list[str]:
    """Contenu de chaque attribut on…="…" (les ${…} peuvent contenir des guillemets : on suit les accolades)."""
    out = []
    for m in re.finditer(r'\son[a-z]+="', html):
        i, depth, buf = m.end(), 0, []
        while i < len(html):
            ch = html[i]
            if html.startswith("${", i):
                depth += 1
                buf.append("${")
                i += 2
                continue
            if ch == "{" and depth:
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
            elif ch == '"' and depth == 0:
                break
            buf.append(ch)
            i += 1
        out.append("".join(buf))
    return out


def _interpolations(text: str) -> list[str]:
    out, i = [], 0
    while (start := text.find("${", i)) != -1:
        depth, j = 1, start + 2
        while j < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        out.append(text[start + 2:j - 1].strip())
        i = j
    return out


# Interpolations sans texte venu des données : constantes, nombres, et l'action de l'accueil (déjà construite avec jsArg).
_LITERAL_TERNARY = re.compile(r'^[\w.!=<>&| ]+\?\s*("[^"$]*"|\d+)\s*:\s*("[^"$]*"|\d+)$')
_SAFE = {"todoAction(t)", "notifyVia ? `${jsArg(notifyVia)}` : \"null\"",
         "p.id ? `openProspect(${jsArg(p.id)})` : \"closeProspectModal()\"", "p.id ? `${jsArg(p.id)}` : \"null\"",
         "c ? `${jsArg(c.id)}` : \"null\""}
# Identifiants (UUID du serveur), index de boucle et booléens : jamais de texte libre.
_ID_OR_FLAG = re.compile(r"^(!?[\w.]+\.(id|active|sending_enabled)|i|[\w.]+_id)$")


@pytest.mark.parametrize("page", sorted(PAGES))
def test_no_free_text_inside_event_handlers(page):
    bad = []
    for handler in _handlers(PAGES[page]):
        if "'${" in handler:
            bad.append(handler)
            continue
        for expr in _interpolations(handler):
            if expr.startswith("jsArg(") or expr in _SAFE or _LITERAL_TERNARY.match(expr) or _ID_OR_FLAG.match(expr):
                continue
            bad.append(f"{expr}  ←  {handler[:120]}")
    assert bad == [], "\n".join(bad)


def test_jsarg_is_json_then_html_escaped_in_both_pages():
    for page in ("dashboard", "superadmin"):
        html = PAGES[page]
        assert 'function jsArg(value) {\n  return esc(JSON.stringify(String(value ?? "")));\n}' in html
        assert html.index("function esc(value)") < html.index("function jsArg(value)")


def test_superadmin_buttons_carry_ids_only():
    sa = PAGES["superadmin"]
    assert 'onclick="toggleActive(${jsArg(t.id)}, ${t.active ? "false" : "true"})"' in sa
    assert 'onclick="openPreregTenant(${jsArg(t.id)})"' in sa and 'onclick="relockPreregTenant(${jsArg(t.id)})"' in sa
    assert 'onclick="downloadPreregFile(${jsArg(f.id)})"' in sa
    for fn in ("toggleActive(tenantId, active)", "openPreregTenant(id)", "relockPreregTenant(id)", "downloadPreregFile(id)"):
        assert f"async function {fn} {{" in sa
    assert "jsArg(t.name)" not in sa and "jsArg(f.filename)" not in sa
    assert "const name = preregTenant(id).name;" in sa and "currentShop.id === tenantId ? currentShop.name" in sa


def test_dashboard_complement_name_is_escaped():
    assert "${esc(l.complement_name)} — ${Number(l.complement_price).toLocaleString()} ${esc(l.complement_currency)}" in PAGES["dashboard"]


# ------------------------------------------------------------------ E1 — Super Admin : limite, code, alerte

@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **mail: sent.append(mail) or True)
    return sent


@pytest.fixture
def sa_mfa(monkeypatch):
    monkeypatch.setattr(get_settings(), "superadmin_mfa", True)


async def _sa(db, active=True) -> SuperAdminUser:
    admin = SuperAdminUser(email=f"sa{uuid.uuid4().hex[:6]}@agencai.ci", hashed_password=hash_password(PASSWORD),
                           full_name="Koffi Yao", active=active)
    db.add(admin)
    await db.commit()
    return admin


def _code(mail: dict) -> str:
    return re.search(r"\n\n(\d{6})\n\n", mail["body"]).group(1)


@pytest.mark.asyncio
async def test_superadmin_login_requires_the_emailed_code(client, db_session, mails, sa_mfa):
    admin = await _sa(db_session)
    r = await client.post(LOGIN, data={"username": admin.email.upper(), "password": PASSWORD})
    assert r.status_code == 200 and r.json()["mfa_required"] is True and r.json()["access_token"] is None
    pending = r.json()["mfa_pending_token"]
    [mail] = mails
    assert mail["to"] == admin.email and mail["subject"] == "Votre code de connexion au Super Admin de Bob"
    code = _code(mail)
    await db_session.refresh(admin)
    assert admin.mfa_otp_code_hash == hashlib.sha256(code.encode()).hexdigest() and code not in admin.mfa_otp_code_hash

    # Le jeton « en attente » n'ouvre rien : ni le Super Admin, ni la vérification des commerçants.
    assert (await client.get("/api/v1/superadmin/overview", headers={"Authorization": f"Bearer {pending}"})).status_code == 401
    r = await client.post("/api/v1/auth/verify-mfa", json={"mfa_pending_token": pending, "code": code})
    assert r.status_code == 401

    wrong = "000000" if code != "000000" else "111111"
    r = await client.post(f"{LOGIN}/verify", json={"mfa_pending_token": pending, "code": wrong})
    assert r.status_code == 401 and r.json()["detail"] == "Code incorrect"

    r = await client.post(f"{LOGIN}/verify", json={"mfa_pending_token": pending, "code": code})
    assert r.status_code == 200 and r.json()["access_token"]
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.get("/api/v1/superadmin/overview", headers=headers)).status_code == 200
    alert = mails[-1]
    assert alert["to"] == admin.email and alert["subject"] == "Bob : nouvelle connexion au Super Admin"
    assert "Adresse : " in alert["body"] and "changez votre mot de passe" in alert["body"]
    await db_session.refresh(admin)
    assert admin.mfa_otp_code_hash is None and admin.last_login_at is not None

    # Le code ne sert qu'une fois.
    r = await client.post(f"{LOGIN}/verify", json={"mfa_pending_token": pending, "code": code})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_superadmin_code_expires_and_attempts_are_limited(client, db_session, mails, sa_mfa):
    admin = await _sa(db_session)
    pending = (await client.post(LOGIN, data={"username": admin.email, "password": PASSWORD})).json()["mfa_pending_token"]
    code = _code(mails[0])
    admin.mfa_otp_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    r = await client.post(f"{LOGIN}/verify", json={"mfa_pending_token": pending, "code": code})
    assert r.status_code == 401 and "expiré" in r.json()["detail"]

    pending = (await client.post(LOGIN, data={"username": admin.email, "password": PASSWORD})).json()["mfa_pending_token"]
    code = _code(mails[-1])
    # L'essai sur le code expiré compte déjà : 4 autres essais, puis plus rien pendant 5 minutes.
    statuses = [(await client.post(f"{LOGIN}/verify", json={"mfa_pending_token": pending, "code": "abcdef"})).status_code
                for _ in range(4)]
    assert statuses == [401] * 4
    r = await client.post(f"{LOGIN}/verify", json={"mfa_pending_token": pending, "code": code})
    assert r.status_code == 429  # même le bon code : 5 essais par 5 minutes


@pytest.mark.asyncio
async def test_superadmin_resend_and_send_failure(client, db_session, monkeypatch, sa_mfa):
    admin = await _sa(db_session)
    sent = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **mail: sent.append(mail) or True)
    pending = (await client.post(LOGIN, data={"username": admin.email, "password": PASSWORD})).json()["mfa_pending_token"]
    assert [(await client.post(f"{LOGIN}/resend", json={"mfa_pending_token": pending})).status_code for _ in range(4)] == [204, 204, 204, 429]
    assert len(sent) == 4 and _code(sent[-1]) != "" and all(m["to"] == admin.email for m in sent)
    # Le dernier code envoyé est le seul valable.
    r = await client.post(f"{LOGIN}/verify", json={"mfa_pending_token": pending, "code": _code(sent[-1])})
    assert r.status_code == 200

    monkeypatch.setattr("app.services.email_service.send_email", lambda **mail: False)
    r = await client.post(LOGIN, data={"username": admin.email, "password": PASSWORD})
    assert r.status_code == 503 and "n'a pas pu être envoyé" in r.json()["detail"]


@pytest.mark.asyncio
async def test_superadmin_login_is_rate_limited_by_email(client, db_session, mails):
    admin = await _sa(db_session)
    statuses = [(await client.post(LOGIN, data={"username": admin.email, "password": "mauvais"})).status_code for _ in range(5)]
    assert statuses == [401] * 5
    r = await client.post(LOGIN, data={"username": admin.email, "password": PASSWORD})
    assert r.status_code == 429  # même le bon mot de passe, pendant 15 minutes
    from app.models.audit_log import AuditLog

    actions = (await db_session.execute(select(AuditLog.action))).scalars().all()
    assert actions.count("SUPERADMIN_LOGIN_FAILED") == 5 and "SUPERADMIN_LOGIN_RATE_LIMITED" in actions


@pytest.mark.asyncio
async def test_superadmin_login_is_rate_limited_by_address(client, db_session, mails):
    for _ in range(10):
        assert (await client.post(LOGIN, data={"username": f"x{uuid.uuid4().hex[:6]}@a.ci", "password": "x"})).status_code == 401
    assert (await client.post(LOGIN, data={"username": "autre@a.ci", "password": "x"})).status_code == 429


@pytest.mark.asyncio
async def test_without_mfa_switch_login_still_alerts(client, db_session, mails):
    admin = await _sa(db_session)  # SUPERADMIN_MFA=false (dépannage) : connexion directe, alerte quand même
    r = await client.post(LOGIN, data={"username": admin.email, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["access_token"] and r.json()["mfa_required"] is False
    assert [m["subject"] for m in mails] == ["Bob : nouvelle connexion au Super Admin"]


def test_superadmin_mfa_is_on_by_default():
    from app.core.config import Settings

    assert Settings.model_fields["superadmin_mfa"].default is True
    assert Settings.model_fields["whatsapp_require_signature"].default is True


def test_superadmin_login_page_has_the_code_step():
    sa = PAGES["superadmin"]
    assert 'id="code-form" class="hidden"' in sa and 'autocomplete="one-time-code"' in sa
    assert "/api/v1/superadmin/login/verify" in sa and "/api/v1/superadmin/login/resend" in sa
    assert "if (res.mfa_required) { mfaPendingToken = res.mfa_pending_token; showCodeForm(true); return; }" in sa


# ------------------------------------------------------------------ F1 — comptes désactivés

@pytest.mark.asyncio
async def test_disabled_superadmin_loses_access_immediately(client, db_session):
    admin = await _sa(db_session)
    headers = {"Authorization": f"Bearer {create_superadmin_access_token(admin.id)}"}
    assert (await client.get("/api/v1/superadmin/tenants", headers=headers)).status_code == 200
    admin.active = False
    await db_session.commit()
    assert (await client.get("/api/v1/superadmin/tenants", headers=headers)).status_code == 401
    ghost = {"Authorization": f"Bearer {create_superadmin_access_token(uuid.uuid4())}"}
    assert (await client.get("/api/v1/superadmin/tenants", headers=ghost)).status_code == 401


@pytest.mark.asyncio
async def test_disabled_or_foreign_user_loses_access_immediately(client, db_session):
    tenant = Tenant(name="Boutique Awa", country="CI", currency="XOF", email=f"{uuid.uuid4().hex[:6]}@l70.ci")
    other = Tenant(name="Autre", country="CI", currency="XOF", email=f"{uuid.uuid4().hex[:6]}@l70.ci")
    db_session.add_all([tenant, other])
    await db_session.commit()
    user = User(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:6]}@l70.ci", hashed_password="x", full_name="Awa", role=Role.ADMIN)
    db_session.add(user)
    await db_session.commit()
    ok = {"Authorization": f"Bearer {create_access_token(user.id, tenant.id, 'ADMIN')}"}
    assert (await client.get("/api/v1/customers", headers=ok)).status_code == 200
    # Même utilisateur, autre boutique dans le jeton : refusé.
    forged = {"Authorization": f"Bearer {create_access_token(user.id, other.id, 'ADMIN')}"}
    assert (await client.get("/api/v1/customers", headers=forged)).status_code == 401
    user.active = False
    await db_session.commit()
    r = await client.get("/api/v1/customers", headers=ok)
    assert r.status_code == 401 and r.json()["detail"] == "Compte désactivé ou introuvable"


# ------------------------------------------------------------------ M2 — signature des messages WhatsApp

def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_webhook_refuses_everything_when_secret_missing(client, db_session, monkeypatch, caplog):
    settings = get_settings()
    monkeypatch.setattr(settings, "whatsapp_app_secret", "")
    monkeypatch.setattr(settings, "whatsapp_require_signature", True)
    body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
    r = await client.post("/webhooks/whatsapp", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 403 and r.json()["detail"] == "Signature webhook impossible à vérifier"
    assert any("WHATSAPP_APP_SECRET n'est pas configuré" in rec.getMessage() and rec.levelname == "ERROR" for rec in caplog.records)

    monkeypatch.setattr(settings, "whatsapp_app_secret", "APP-SECRET")
    r = await client.post("/webhooks/whatsapp", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 403 and r.json()["detail"] == "Signature webhook invalide"
    r = await client.post("/webhooks/whatsapp", content=body,
                          headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign("APP-SECRET", body)})
    assert r.status_code == 200

    monkeypatch.setattr(settings, "whatsapp_app_secret", "")
    monkeypatch.setattr(settings, "whatsapp_require_signature", False)  # développement local seulement
    r = await client.post("/webhooks/whatsapp", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200


# ------------------------------------------------------------------ M1 — démo publique

CSV = b"Nom,Prix,Description,Stock\nSamsung A56,280000,256 Go,12\n"


async def _demo(client):
    files = {"file": ("catalogue.csv", io.BytesIO(CSV), "text/csv")}
    return await client.post("/api/v1/demo/create", data={"company_name": "Boutique Démo", "currency": "XOF"}, files=files)


@pytest.mark.asyncio
async def test_demo_creation_is_limited_per_address(client, db_session):
    statuses = [(await _demo(client)).status_code for _ in range(6)]
    assert statuses == [200] * 5 + [429]
    assert "Réessaie dans une heure" in (await _demo(client)).json()["detail"]


@pytest.mark.asyncio
async def test_demo_creation_has_a_daily_ceiling(client, db_session, monkeypatch):
    from app.api.demo import routes

    monkeypatch.setattr(routes, "DEMO_CREATE_PER_IP", 100)
    monkeypatch.setattr(routes, "DEMO_CREATE_PER_DAY", 2)
    assert [(await _demo(client)).status_code for _ in range(3)] == [200, 200, 429]


@pytest.mark.asyncio
async def test_demo_chat_stops_calling_the_ai_after_30_messages(client, db_session):
    from app.api.demo.routes import DEMO_MAX_MESSAGES

    created = (await _demo(client)).json()
    headers = {"Authorization": f"Bearer {created['demo_token']}"}
    fake = FakeLLMClient([text_response(f"Réponse {i}") for i in range(DEMO_MAX_MESSAGES)])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        for i in range(DEMO_MAX_MESSAGES):
            r = await client.post("/api/v1/demo/chat", headers=headers, json={"message": f"Bonjour {i}"})
            assert r.status_code == 200 and r.json()["reply"] == f"Réponse {i}"
        calls = fake.call_count
        r = await client.post("/api/v1/demo/chat", headers=headers, json={"message": "Encore une question"})
        assert r.status_code == 200 and r.json()["reply"].startswith("Tu as atteint la limite de 30 messages")
        assert fake.call_count == calls  # aucun appel à l'IA
        too_long = await client.post("/api/v1/demo/chat", headers=headers, json={"message": "x" * 1001})
        assert too_long.status_code == 400 and too_long.json()["detail"] == "Message trop long : 1000 caractères au plus."
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    stored = (await db_session.execute(select(Message).where(Message.tenant_id == uuid.UUID(created["tenant_id"]),
                                                             Message.content == "Encore une question"))).scalars().all()
    assert stored == []


def test_demo_input_is_capped():
    assert '<input id="chat-input" maxlength="1000"' in PAGES["instant-demo"]


# ------------------------------------------------------------------ E2 — vraie adresse du visiteur

def test_uvicorn_trusts_only_nginx_for_the_visitor_address():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '"--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,172.16.0.0/12"]' in docker


@pytest.mark.asyncio
async def test_proxy_headers_take_the_address_added_by_nginx():
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["client"][0])

    mw = ProxyHeadersMiddleware(inner, trusted_hosts="127.0.0.1,172.16.0.0/12")

    async def call(client_ip, xff):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        await mw({"type": "http", "client": (client_ip, 1234), "headers": headers, "scheme": "http"}, None, None)

    await call("172.18.0.1", "41.66.1.2")                 # nginx (réseau Docker) → le visiteur
    await call("172.18.0.1", "6.6.6.6, 41.66.1.2")        # en-tête inventé par le visiteur : ignoré
    await call("8.8.8.8", "41.66.1.2")                    # pas nginx : on ne croit pas l'en-tête
    assert seen == ["41.66.1.2", "41.66.1.2", "8.8.8.8"]


# ------------------------------------------------------------------ F4 — carte de l'API

@pytest.mark.asyncio
async def test_api_map_is_hidden_outside_development(client):
    assert get_settings().debug is False
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404


def test_merchant_pending_token_cannot_finish_superadmin_login():
    # Les deux jetons « en attente » ne sont pas interchangeables.
    from fastapi import HTTPException

    from app.core.security import decode_mfa_pending_token, decode_superadmin_mfa_pending_token

    with pytest.raises(HTTPException):
        decode_superadmin_mfa_pending_token(create_mfa_pending_token(uuid.uuid4()))
    with pytest.raises(HTTPException):
        decode_mfa_pending_token(create_superadmin_mfa_pending_token(uuid.uuid4()))
