"""
Lot 134 (27/11, décisions de Thérèse sur les 4 points de Naomy, contrat 1.7.4) :
- les liens « une seule page » ne s'usent plus à l'ouverture (échange au toucher, bouton « Redemander le lien à Bob ») ;
- 409 boutique_existante : `trouve_par` ; POST /appairage/reponse : accepter dans WhatsApp, depuis le numéro relié seulement ;
- choix B : e-mail facultatif ; donné, il attend sa confirmation (un seul e-mail) et ne bloque jamais personne.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models.naomy import NaomyPairingCode, NaomyPairingRequest
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import email_confirm, email_service, page_links
from app.services import naomy_bridge as nb
from app.tests.test_lot89_connecteur_naomy import _shop, _signed, conforms, naomy_env  # noqa: F401
from app.tests.test_lot118_equipe_naomy import NUMBER, _create, limiter  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **kw: sent.append(kw) or True)
    return sent


async def _owner(db, tenant):
    return (await db.execute(select(User).where(User.tenant_id == tenant.id, User.role == Role.OWNER))).scalar_one()


def _token(mail) -> str:
    return re.search(r"jeton=([A-Za-z0-9_\-]+)", mail["body"]).group(1)


# ------------------------------------------------------------------ e-mail facultatif (choix B)

@pytest.mark.asyncio
@pytest.mark.parametrize("email", [..., None, ""])
async def test_account_without_email_is_created_and_nothing_is_mailed(client, db_session, naomy_env, limiter, mails, email):
    r = await _create(client, email=email)
    assert r.status_code == 201, r.text
    conforms(r.json(), "CompteCree")
    tenant = (await db_session.execute(select(Tenant))).scalar_one()
    owner = await _owner(db_session, tenant)
    assert tenant.email == owner.email == "wa2250701020304@attente.invalid" and tenant.email_pending is None
    assert mails == []


@pytest.mark.asyncio
async def test_a_given_email_waits_and_gets_one_confirmation_mail(client, db_session, naomy_env, limiter, mails):
    assert (await _create(client, email="  Awa@Maquis.ci ")).status_code == 201
    tenant = (await db_session.execute(select(Tenant))).scalar_one()
    owner = await _owner(db_session, tenant)
    assert tenant.email_pending == "awa@maquis.ci" and email_confirm.is_placeholder(owner.email)
    assert email_confirm.is_placeholder(tenant.email)
    [mail] = mails
    assert mail["to"] == "awa@maquis.ci" and mail["subject"] == "Confirmez votre adresse pour Bob"
    assert "https://agenc-ai.com/bob/email/confirmer?jeton=" in mail["body"] and "7 jours" in mail["body"]
    assert tenant.email_confirm_hash and tenant.email_confirm_hash != _token(mail)  # seule l'empreinte est gardée


def test_bad_email_is_still_refused():
    from app.services.naomy_accounts import validate

    base = {"compte_naomy": "n", "numero_whatsapp": NUMBER, "nom_boutique": "M", "secteur": "RESTAURANT", "pays": "CI",
            "nom_contact": "A", "url_evenements": "https://agenc-ai.com/naomy/api/bob/e"}
    assert validate(base)["email"] is None and validate({**base, "email": None})["email"] is None
    for bad in ("pas-une-adresse", 12, "a@b", "x" * 250 + "@b.ci"):
        with pytest.raises(nb.NaomyError):
            validate({**base, "email": bad})


def test_nothing_is_ever_sent_to_a_placeholder(monkeypatch):
    opened = []

    def smtp(*a, **kw):
        opened.append(a)
        raise OSError("pas de serveur")

    monkeypatch.setattr(email_service.get_settings(), "smtp_host", "smtp.example")
    monkeypatch.setattr(email_service.smtplib, "SMTP", smtp)
    assert email_service.send_email(to="wa2250701020304@attente.invalid", subject="s", body="b") is False
    assert email_service.send_email(to=" X@Attente.INVALID ", subject="s", body="b") is False
    assert opened == []  # aucune connexion SMTP
    email_service.send_email(to="awa@maquis.ci", subject="s", body="b")
    assert len(opened) == 1  # une vraie adresse, elle, part


@pytest.mark.asyncio
async def test_opening_the_link_confirms_nothing_the_button_does(client, db_session, naomy_env, limiter, mails):
    await _create(client)
    token = _token(mails[0])
    page = await client.get(f"/email/confirmer?jeton={token}")
    assert page.status_code == 200 and "Confirmer mon adresse" in page.text and 'method="post"' in page.text
    assert page.headers["cache-control"] == "no-store"
    tenant = (await db_session.execute(select(Tenant))).scalar_one()
    await db_session.refresh(tenant)
    assert tenant.email_pending == "awa@maquis.ci"  # l'ouverture (aperçu, antivirus) ne confirme rien
    done = await client.post("/email/confirmer", data={"jeton": token})
    assert done.status_code == 200 and "Adresse confirmée" in done.text
    await db_session.refresh(tenant)
    owner = await _owner(db_session, tenant)
    await db_session.refresh(owner)
    assert tenant.email == owner.email == "awa@maquis.ci" and tenant.email_pending is None
    again = await client.post("/email/confirmer", data={"jeton": token})
    assert "expiré ou déjà utilisé" in again.text  # usage unique
    assert "expiré" in (await client.post("/email/confirmer", data={"jeton": "x" * 40})).text


@pytest.mark.asyncio
async def test_confirmation_expires_after_7_days(db_session, mails):
    tenant, _ = await _shop(db_session)
    now = datetime.now(timezone.utc)
    await email_confirm.start(db_session, tenant, "awa@maquis.ci", now - timedelta(days=7, minutes=1), mailer=lambda **kw: mails.append(kw))
    assert await email_confirm.confirm(db_session, _token(mails[0]), now) == "inconnu"
    await email_confirm.start(db_session, tenant, "awa@maquis.ci", now - timedelta(days=6), mailer=lambda **kw: mails.append(kw))
    assert await email_confirm.confirm(db_session, _token(mails[1]), now) == "ok"


@pytest.mark.asyncio
async def test_a_pending_address_never_blocks_its_real_owner(client, db_session, naomy_env, limiter, mails):
    await _create(client)  # quelqu'un donne awa@maquis.ci sans la posséder
    other = await _create(client, numero_whatsapp="+2250709090909", compte_naomy="nao_x")
    assert other.status_code == 201, other.text  # l'adresse non confirmée ne déclenche pas de 409
    # Le vrai propriétaire crée son compte Bob avec : la première confirmation échoue alors (« prise »).
    real, _ = await _shop(db_session, email="awa@maquis.ci")
    assert await email_confirm.confirm(db_session, _token(mails[0])) == "prise"
    shops = (await db_session.execute(select(Tenant).where(Tenant.id != real.id))).scalars().all()
    assert all(email_confirm.is_placeholder(t.email) for t in shops)


@pytest.mark.asyncio
async def test_preregistration_welcome_waits_for_the_confirmation(client, db_session, naomy_env, limiter, mails, monkeypatch):
    from app.services import preregistration

    async def on(db):
        return True

    welcomed = []

    async def welcome(db, tenant, owner, send=None):
        welcomed.append(owner.email)

    monkeypatch.setattr(preregistration, "mode_on", on)
    monkeypatch.setattr(preregistration, "send_signup_emails", welcome)
    assert (await _create(client)).json()["pre_inscrit"] is True
    assert welcomed == [] and len(mails) == 1
    assert await email_confirm.confirm(db_session, _token(mails[0])) == "ok"
    assert welcomed == ["awa@maquis.ci"]


# ------------------------------------------------------------------ 409 : trouve_par

@pytest.mark.asyncio
@pytest.mark.parametrize("where,expected", [("whatsapp", "whatsapp_relie"), ("phone", "telephone"), ("email", "email")])
async def test_conflict_says_how_the_shop_was_found(client, db_session, naomy_env, limiter, mails, where, expected):
    tenant, _ = await _shop(db_session, email="vieux@shop.ci")
    if where == "whatsapp":
        db_session.add(WhatsAppAccount(tenant_id=tenant.id, phone_number_id="pn-134", waba_id="w",
                                       display_phone_number="+225 07 01 02 03 04", system_user_token="t"))
        tenant.phone = "+225 07 01 02 03 04"  # relié ET saisi : le WhatsApp relié l'emporte
    elif where == "phone":
        tenant.phone = "07 01 02 03 04"
    await db_session.commit()
    r = await _create(client, **({"email": "vieux@shop.ci"} if where == "email" else {}))
    assert r.status_code == 409 and r.json()["erreur"] == "boutique_existante" and r.json()["trouve_par"] == expected
    conforms(r.json(), "Erreur")


# ------------------------------------------------------------------ accepter dans WhatsApp

async def _waiting(db, linked=True, account="nao_8f2c", age=timedelta(0), plan=TenantPlan.PRO):
    tenant, _ = await _shop(db, plan=plan)
    if linked:
        db.add(WhatsAppAccount(tenant_id=tenant.id, phone_number_id=f"pn-{tenant.id.hex[:6]}", waba_id="w",
                               display_phone_number="+225 07 01 02 03 04", system_user_token="t"))
    else:
        tenant.phone = NUMBER  # numéro saisi seulement : jamais assez
    request = NaomyPairingRequest(tenant_id=tenant.id, naomy_account=account, shop_name="Maquis",
                                  created_at=datetime.now(timezone.utc) - age)
    db.add(request)
    await db.commit()
    return tenant, request


async def _reply(client, **over):
    body = {"compte_naomy": "nao_8f2c", "numero_whatsapp": NUMBER, "decision": "accepter", **over}
    raw, headers = _signed(body)
    return await client.post("/api/v1/naomy/appairage/reponse", content=raw, headers=headers)


@pytest.mark.asyncio
async def test_accepting_in_whatsapp_from_the_linked_number_gives_the_pairing_code(client, db_session, naomy_env):
    tenant, request = await _waiting(db_session)
    r = await _reply(client)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["statut"] == "acceptee" and data["portee"] == "complete" and len(data["code"]) == nb.CODE_LENGTH
    assert data["lien"] == f"https://agenc-ai.com/naomy/connexion/bob?code={data['code']}"
    await db_session.refresh(request)
    assert request.status == "ACCEPTED"
    code = (await db_session.execute(select(NaomyPairingCode))).scalar_one()
    assert code.tenant_id == tenant.id and code.code_hash == nb.digest(data["code"]) and code.scope == "complete"
    raw, headers = _signed({"code": data["code"], "compte_naomy": "nao_8f2c", "numero_confirmation": NUMBER,
                            "url_evenements": "https://agenc-ai.com/naomy/api/bob/evenements"})
    assert (await client.post("/api/v1/naomy/appairage", content=raw, headers=headers)).status_code == 200
    assert (await _reply(client)).status_code == 404  # déjà traitée


@pytest.mark.asyncio
async def test_a_small_plan_gets_the_assistant_only(client, db_session, naomy_env):
    await _waiting(db_session, plan=TenantPlan.FREE)
    data = (await _reply(client)).json()
    assert data["statut"] == "acceptee" and data["portee"] == "assistant"


@pytest.mark.asyncio
async def test_refusing_in_whatsapp(client, db_session, naomy_env):
    _, request = await _waiting(db_session)
    r = await _reply(client, decision="refuser")
    assert r.status_code == 200 and r.json() == {"statut": "refusee"}
    await db_session.refresh(request)
    assert request.status == "REFUSED"
    assert (await db_session.execute(select(func.count(NaomyPairingCode.id)))).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["typed_phone_only", "other_naomy_account", "too_old", "other_number", "two_shops"])
async def test_no_acceptance_unless_the_linked_number_answers_a_waiting_request(client, db_session, naomy_env, case):
    if case == "typed_phone_only":
        _, request = await _waiting(db_session, linked=False)
    elif case == "other_naomy_account":
        _, request = await _waiting(db_session, account="nao_autre")
    elif case == "too_old":
        _, request = await _waiting(db_session, age=timedelta(days=7, minutes=1))
    else:
        _, request = await _waiting(db_session)
        if case == "two_shops":
            await _waiting(db_session)
    r = await _reply(client, **({"numero_whatsapp": "+2250709090909"} if case == "other_number" else {}))
    assert r.status_code == 404 and r.json()["erreur"] == "demande_introuvable"
    await db_session.refresh(request)
    assert request.status == "PENDING"
    assert (await db_session.execute(select(func.count(NaomyPairingCode.id)))).scalar_one() == 0


@pytest.mark.asyncio
async def test_reply_route_needs_the_platform_signature_and_clean_fields(client, db_session, naomy_env):
    await _waiting(db_session)
    raw, headers = _signed({"compte_naomy": "nao_8f2c", "numero_whatsapp": NUMBER, "decision": "accepter"}, secret="faux")
    assert (await client.post("/api/v1/naomy/appairage/reponse", content=raw, headers=headers)).status_code == 401
    for over in ({"decision": "oui"}, {"numero_whatsapp": "0701020304"}, {"compte_naomy": ""}, {"portee": "complete"}):
        assert (await _reply(client, **over)).status_code == 400
    assert (await db_session.execute(select(func.count(NaomyPairingCode.id)))).scalar_one() == 0


# ------------------------------------------------------------------ pages « une seule page »

def test_page_link_carries_the_agencai_number_of_the_country(naomy_env, monkeypatch):
    monkeypatch.setattr(naomy_env, "naomy_whatsapp_numbers", "CI=+2250509777333")
    assert page_links.page_url("carte", "T", "CI") == "https://agenc-ai.com/bob/m/?page=carte&connexion=T&wa=2250509777333"
    assert page_links.page_url("carte", "T", "CM").endswith("&connexion=T&wa=33780841269")


def test_the_page_exchanges_the_link_only_on_tap():
    html = (ROOT / "app" / "static" / "m" / "index.html").read_text(encoding="utf-8")
    start = html[html.index("async function start()"):html.index("// ---------------------------------------------------------------- Ma carte")]
    tap = start[start.index('go.addEventListener("click"'):]
    assert "/auth/magic" in tap and start.count("/auth/magic") == 1  # le seul échange est dans le toucher
    assert 'OPEN = { carte: "Ouvrir ma carte", horaires: "Ouvrir mes horaires", affiches: "Ouvrir mes affiches" }' in html
    assert "Redemander le lien à Bob" in html and '"https://wa.me/" + WA + "?text=" + encodeURIComponent("menu")' in html
    assert "/^[1-9][0-9]{7,14}$/.test" in html  # seulement des chiffres dans le lien wa.me


def test_contract_174_keeps_old_calls_valid():
    from app.tests.test_lot89_connecteur_naomy import _contract

    doc = _contract()
    creation = doc["components"]["schemas"]["CreationCompte"]
    assert "email" not in creation["required"]
    reply = doc["paths"]["/appairage/reponse"]["post"]
    assert set(reply["requestBody"]["content"]["application/json"]["schema"]["required"]) == {
        "compte_naomy", "numero_whatsapp", "decision"}
    assert "1.7.4" in doc["info"]["description"]


def test_after_the_tap_the_used_link_never_comes_back():
    """Lot 134 b — essai réel du 27/11 : après l'échange, start() relisait l'ancienne adresse et redemandait le toucher."""
    html = (ROOT / "app" / "static" / "m" / "index.html").read_text(encoding="utf-8")
    start = html[html.index("async function start()"):]
    read, drop, store = (start.index('params.get("connexion")'), start.index('params.delete("connexion")'),
                         start.index('sessionStorage.setItem(KEY + "_lien", fresh)'))
    assert read < drop < store  # lu une fois, retiré aussitôt, avant d'être gardé
    assert 'sessionStorage.removeItem(KEY + "_lien")' in start  # effacé dès la réponse
