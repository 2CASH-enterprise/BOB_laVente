"""
Lot 120 — interrupteur entre les Super Admin de Bob et de Naomy (contrat 1.5, section « console ») : jeton de passage
signé (60 s, usage unique) dans les deux sens ; Bob fait foi pour l'équipe (membre retrouvé par e-mail, rôles de Bob) ;
chaque changement d'un membre part vers Naomy, signé, avec renvois ; Naomy peut relire toute l'équipe.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.console import ConsoleEvent, ConsoleJump
from app.models.superadmin_user import SuperAdminUser
from app.services import console_bridge as cb
from app.services import naomy_bridge as nb
from app.tests.test_lot89_connecteur_naomy import SECRET, naomy_env  # noqa: F401
from app.tests.test_lot119_roles_super_admin import _h, _member

HTML = Path("app/static/superadmin/index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _from_naomy(member, secret=SECRET, now=None, audience="bob", issuer="naomy"):
    return cb.make_token(secret, member, audience, now=now, issuer=issuer)


def test_the_token_format_and_its_checks():
    m = SuperAdminUser(email="koffi@agencai.ci", full_name="Koffi", roles=["SALES", "TECH"], active=True, hashed_password="x")
    m.id = __import__("uuid").uuid4()
    token = cb.make_token(SECRET, m, "naomy", now=NOW)
    body, sig = token.split(".")
    payload = json.loads(cb._unb64(body))
    assert payload["iss"] == "bob" and payload["aud"] == "naomy" and payload["exp"] - payload["iat"] == 60
    assert payload["membre"] == {"id": str(m.id), "email": "koffi@agencai.ci", "nom": "Koffi", "roles": ["SALES", "TECH"], "actif": True}
    assert cb.read_token(SECRET, token, "naomy", NOW + timedelta(seconds=59))["jti"] == payload["jti"]
    for bad, when, aud in [(token, NOW + timedelta(seconds=91), "naomy"), (token, NOW - timedelta(seconds=31), "naomy"),
                           (token, NOW, "bob"), (body + "." + sig[:-2] + "AA", NOW, "naomy"), ("abc", NOW, "naomy"),
                           (cb.make_token("autre", m, "naomy", now=NOW), NOW, "naomy"), ("a.b.c", NOW, "naomy")]:
        with pytest.raises(cb.ConsoleError):
            cb.read_token(SECRET, bad, aud, when)
    with pytest.raises(cb.ConsoleError):  # passerelle éteinte : jamais de jeton accepté, même signé « à vide »
        cb.read_token("", cb.make_token("", m, "naomy", now=NOW), "naomy", NOW)
    # Un jeton bien signé mais valable plus de 60 secondes est refusé.
    long = dict(payload, exp=payload["iat"] + 3600)
    body2 = cb._b64(json.dumps(long).encode())
    with pytest.raises(cb.ConsoleError):
        cb.read_token(SECRET, f"{body2}.{cb._sig(SECRET, body2)}", "naomy", NOW + timedelta(seconds=10))
    # Signature recalculable par Naomy : HMAC-SHA256(secret, "console." + charge), base64url sans « = ».
    import base64
    import hashlib
    import hmac
    expected = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), f"console.{body}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert sig == expected


@pytest.mark.asyncio
async def test_switch_to_naomy(client, db_session, naomy_env):
    m = await _member(db_session, "SALES", name="Koffi Yao")
    agents = (await client.get("/api/v1/superadmin/console/agents", headers=_h(m))).json()["agents"]
    assert [(a["code"], a["actuel"], a["disponible"]) for a in agents] == [("bob", True, True), ("naomy", False, True)]
    r = await client.post("/api/v1/superadmin/console/sortie", headers=_h(m), json={"agent": "naomy"})
    url = r.json()["url"]
    assert url.startswith("https://agenc-ai.com/naomy/superadmin/?jeton=")
    payload = cb.read_token(SECRET, url.split("jeton=")[1], "naomy")
    assert payload["membre"]["roles"] == ["SALES"] and payload["membre"]["nom"] == "Koffi Yao"
    assert (await client.post("/api/v1/superadmin/console/sortie", headers=_h(m), json={"agent": "autre"})).status_code == 404
    assert (await client.post("/api/v1/superadmin/console/sortie", json={"agent": "naomy"})).status_code == 401
    naomy_env.naomy_console_url = "https://console.exemple/naomy?x=1"
    url = (await client.post("/api/v1/superadmin/console/sortie", headers=_h(m), json={"agent": "naomy"})).json()["url"]
    assert url.startswith("https://console.exemple/naomy?x=1&jeton=")


@pytest.mark.asyncio
async def test_no_switch_while_naomy_is_not_linked(client, db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "naomy_platform_secret", "")
    m = await _member(db_session, "OWNER")
    agents = (await client.get("/api/v1/superadmin/console/agents", headers=_h(m))).json()["agents"]
    assert agents[1]["disponible"] is False
    assert (await client.post("/api/v1/superadmin/console/sortie", headers=_h(m), json={"agent": "naomy"})).status_code == 404


@pytest.mark.asyncio
async def test_entry_from_naomy_uses_bobs_roles_and_works_once(client, db_session, naomy_env):
    m = await _member(db_session, "TECH", name="Awa")
    forged = SuperAdminUser(id=m.id, email=m.email.upper(), full_name="Awa", roles=["OWNER"], active=True, hashed_password="x")
    token = _from_naomy(forged)  # Naomy ne peut pas donner plus de droits : ce sont ceux de Bob
    r = await client.post("/api/v1/superadmin/console/entree", json={"jeton": token})
    assert r.status_code == 200
    me = (await client.get("/api/v1/superadmin/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})).json()
    assert me["roles"] == ["TECH"]
    again = await client.post("/api/v1/superadmin/console/entree", json={"jeton": token})
    assert again.status_code == 401 and "expiré" in again.json()["detail"]
    assert (await db_session.execute(select(ConsoleJump))).scalars().all()


@pytest.mark.asyncio
async def test_entry_refusals(client, db_session, naomy_env):
    m = await _member(db_session, "SALES")
    off = await _member(db_session, "SALES", active=False)
    invited = await _member(db_session, "SALES")
    invited.invite_token_hash = "x" * 64
    await db_session.commit()
    stranger = SuperAdminUser(email="inconnu@x.ci", full_name="X", roles=["OWNER"], active=True, hashed_password="x")
    stranger.id = __import__("uuid").uuid4()
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    for token in (_from_naomy(off), _from_naomy(invited), _from_naomy(stranger), _from_naomy(m, secret="autre"),
                  _from_naomy(m, now=old), _from_naomy(m, audience="naomy"), _from_naomy(m, issuer="bob"), "n'importe quoi"):
        assert (await client.post("/api/v1/superadmin/console/entree", json={"jeton": token})).status_code == 401


@pytest.mark.asyncio
async def test_member_changes_go_to_naomy_signed(client, db_session, naomy_env, monkeypatch):
    monkeypatch.setattr("app.services.email_service.send_email", lambda **kw: True)
    owner = await _member(db_session, "OWNER", name="Thérèse")
    koffi = await _member(db_session, "SALES", name="Koffi")
    await client.put(f"/api/v1/superadmin/team/{koffi.id}", headers=_h(owner), json={"roles": ["SALES", "MARKETING"]})
    await client.put(f"/api/v1/superadmin/team/{koffi.id}", headers=_h(owner), json={"active": False})
    await client.put(f"/api/v1/superadmin/team/{koffi.id}", headers=_h(owner), json={"roles": ["SALES", "MARKETING"]})  # rien ne change
    r = await client.post("/api/v1/superadmin/team/invite", headers=_h(owner), json={"email": "awa@x.ci", "full_name": "Awa", "roles": ["TECH"]})
    awa = await db_session.get(SuperAdminUser, __import__("uuid").UUID(r.json()["id"]))
    await client.put(f"/api/v1/superadmin/team/{awa.id}", headers=_h(owner), json={"roles": ["BILLING"]})  # pas encore membre : rien
    sent = []

    async def sender(url, body, headers):
        sent.append((url, body, headers))
        return 200

    report = await cb.dispatch(db_session, sender=sender)
    assert report == {"sent": 2, "retry": 0, "failed": 0}
    first, second = (json.loads(b) for _, b, _ in sent)
    assert first["type"] == "membre.mis_a_jour" and first["raison"] == "roles" and first["membre"]["roles"] == ["SALES", "MARKETING"]
    assert second["raison"] == "desactive" and second["membre"]["actif"] is False
    url, body, headers = sent[0]
    assert url == "https://agenc-ai.com/naomy/api/bob/console/membres"
    assert nb.platform_signature_ok(SECRET, headers["X-Platform-Timestamp"], headers["X-Platform-Signature"], body)
    assert (await cb.dispatch(db_session, sender=sender))["sent"] == 0  # déjà parti


@pytest.mark.asyncio
async def test_joining_is_sent_and_retries(client, db_session, naomy_env, monkeypatch):
    import re

    mails = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **kw: mails.append(kw) or True)
    owner = await _member(db_session, "OWNER")
    await client.post("/api/v1/superadmin/team/invite", headers=_h(owner), json={"email": "awa@x.ci", "full_name": "Awa", "roles": ["TECH"]})
    token = re.search(r"invitation=([\w-]+)", mails[-1]["body"]).group(1)
    await client.post("/api/v1/superadmin/team/accept", json={"token": token, "password": "un-bon-mot-de-passe"})

    async def down(url, body, headers):
        return 503

    now = datetime.now(timezone.utc)
    assert (await cb.dispatch(db_session, now=now, sender=down))["retry"] == 1
    evt = (await db_session.execute(select(ConsoleEvent))).scalar_one()
    assert evt.data["raison"] == "rejoint" and evt.data["membre"]["actif"] is True and evt.attempts == 1
    assert (await cb.dispatch(db_session, now=now, sender=down))["retry"] == 0  # pas avant l'heure du renvoi
    for minutes in (1, 6, 40, 200, 1000):
        await cb.dispatch(db_session, now=now + timedelta(minutes=minutes * 2), sender=down)
    await db_session.refresh(evt)
    assert evt.status == "FAILED" and evt.attempts == 6
    purged = await cb.purge(db_session, now=now + timedelta(days=31))
    assert purged["events"] == 1


@pytest.mark.asyncio
async def test_nothing_queued_without_the_gateway(db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "naomy_platform_secret", "")
    m = await _member(db_session, "OWNER")
    cb.queue_member(db_session, m, "roles")
    await db_session.commit()
    assert not (await db_session.execute(select(ConsoleEvent))).scalars().all()
    assert await cb.dispatch(db_session) == {"sent": 0, "retry": 0, "failed": 0}


@pytest.mark.asyncio
async def test_naomy_reads_the_whole_team(client, db_session, naomy_env):
    await _member(db_session, "OWNER", name="Thérèse")
    await _member(db_session, "SALES", name="Ancien", active=False)
    invited = await _member(db_session, "TECH", name="Pas encore")
    invited.invite_token_hash = "y" * 64
    await db_session.commit()
    stamp = str(int(time.time()))
    headers = {"X-Platform-Timestamp": stamp, "X-Platform-Signature": nb.sign(SECRET, stamp, b"")}
    r = await client.get("/api/v1/naomy/console/membres", headers=headers)
    assert r.status_code == 200
    members = {m["nom"]: m for m in r.json()["membres"]}
    assert set(members) == {"Thérèse", "Ancien"} and members["Ancien"]["actif"] is False and members["Thérèse"]["roles"] == ["OWNER"]
    bad = {"X-Platform-Timestamp": stamp, "X-Platform-Signature": nb.sign("autre", stamp, b"")}
    assert (await client.get("/api/v1/naomy/console/membres", headers=bad)).status_code == 401


@pytest.mark.asyncio
async def test_old_jumps_are_cleaned(db_session):
    db_session.add_all([ConsoleJump(jti="a", expires_at=NOW - timedelta(minutes=1)), ConsoleJump(jti="b", expires_at=NOW + timedelta(minutes=1))])
    await db_session.commit()
    assert (await cb.purge(db_session, now=NOW))["jumps"] == 1


def test_the_console_has_the_switch_and_the_entry():
    assert 'id="agent-switch"' in HTML and "function switchConsole(" in HTML and "/console/sortie" in HTML
    assert 'get("jeton")' in HTML and "/console/entree" in HTML and "history.replaceState" in HTML
    assert "loadAgentSwitch();" in HTML


@pytest.mark.asyncio
async def test_what_bob_sends_matches_the_contract(client, db_session, naomy_env):
    from app.tests.test_lot89_connecteur_naomy import conforms

    owner = await _member(db_session, "OWNER")
    koffi = await _member(db_session, "SALES")
    await client.put(f"/api/v1/superadmin/team/{koffi.id}", headers=_h(owner), json={"roles": ["SALES", "TECH"]})
    evt = (await db_session.execute(select(ConsoleEvent))).scalar_one()
    conforms(evt.data, "EvenementMembre")
    conforms(cb.member_view(koffi), "MembreConsole")
