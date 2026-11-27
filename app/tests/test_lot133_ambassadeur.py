"""
Lot 133 — rôle « Ambassadeur » (25/11) : une personne extérieure, payée à la commission, qui prospecte ses propres
contacts. Elle ne voit et ne touche que les prospects qu'elle a ajoutés ; ni vue d'ensemble, ni boutiques, ni import,
export, campagnes, réglages ; rien chez Naomy. Commissions : lot suivant (quand les paiements seront enregistrés).
"""
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.prospect import Prospect
from app.services import console_bridge, sa_roles
from app.tests.test_lot119_roles_super_admin import _h, _member

API = "/api/v1/superadmin/prospects"


def _p(code, owner, **over):
    values = dict(company=f"Maquis {code}", contact_name="Awa", sector="RESTAURANT", code=code, country="CI",
                  phone=f"22507010{code[-4:]}", status="ACTIVE", owner_id=owner)
    values.update(over)
    return Prospect(**values)


@pytest.mark.asyncio
async def test_ambassador_sees_and_touches_only_own_prospects(client, db_session):
    amb, other = await _member(db_session, "AMBASSADOR", name="Kofi Ambassadeur"), await _member(db_session, "SALES", name="Noé")
    db_session.add_all([_p("aa1111", amb.id, sector="ONLINE_STORE"), _p("bb2222", other.id, sector="ONLINE_STORE")])
    await db_session.commit()
    mine = (await db_session.execute(select(Prospect).where(Prospect.code == "aa1111"))).scalar_one()
    theirs = (await db_session.execute(select(Prospect).where(Prospect.code == "bb2222"))).scalar_one()
    h = _h(amb)
    listed = (await client.get(API, headers=h)).json()
    assert [p["company"] for p in listed["prospects"]] == ["Maquis aa1111"] and [o["name"] for o in listed["owners"]] == ["Kofi Ambassadeur"]
    assert (await client.get(f"{API}?owner={other.id}", headers=h)).json()["prospects"][0]["company"] == "Maquis aa1111"
    assert (await client.get(f"{API}/{mine.id}", headers=h)).status_code == 200
    for method, path, body in [("get", f"{API}/{theirs.id}", None), ("put", f"{API}/{theirs.id}", {"company": "X"}),
                               ("post", f"{API}/{theirs.id}/contact", {"channel": "WHATSAPP"}),
                               ("post", f"{API}/{theirs.id}/status", {"status": "NOT_INTERESTED"}),
                               ("post", f"{API}/{theirs.id}/note", {"note": "x"})]:
        r = await getattr(client, method)(path, headers=h, **({"json": body} if body else {}))
        assert r.status_code == 404, path
    assert (await client.post(f"{API}/{mine.id}/contact", json={"channel": "WHATSAPP"}, headers=h)).status_code == 200
    today = (await client.get(f"{API}/today", headers=h)).json()
    assert today["restaurants"] is None and today["sent_today"] == 1 and today["to_send"] == []  # jamais le prospect d'un autre
    assert (await client.get(f"{API}/funnel", headers=h)).json()["total"] == 1


@pytest.mark.asyncio
async def test_ambassador_adds_own_prospects_and_learns_nothing_from_duplicates(client, db_session):
    amb, other = await _member(db_session, "AMBASSADOR"), await _member(db_session, "SALES")
    db_session.add(_p("cc3333", other.id, company="Maquis Secret", phone="2250701999999"))
    await db_session.commit()
    h = _h(amb)
    r = await client.post(API, json={"company": "Chez Kofi", "sector": "RESTAURANT", "country": "CI", "phone": "+2250701888888"}, headers=h)
    assert r.status_code == 201 and r.json()["owner_id"] == str(amb.id)
    dup = await client.post(API, json={"company": "Autre", "sector": "RESTAURANT", "country": "CI", "phone": "+2250701999999"}, headers=h)
    assert dup.status_code == 409 and "Secret" not in dup.text and "déjà suivi" in dup.json()["detail"]
    moved = await client.put(f"{API}/{r.json()['id']}", json={"owner_id": str(other.id)}, headers=h)
    assert moved.status_code == 200 and moved.json()["owner_id"] == str(amb.id)  # il ne peut pas le donner à un autre


@pytest.mark.asyncio
async def test_ambassador_is_kept_out_of_everything_else(client, db_session):
    h = _h(await _member(db_session, "AMBASSADOR"))
    me = (await client.get("/api/v1/superadmin/me", headers=h)).json()
    assert me["role_labels"] == ["Ambassadeur"] and "prospects.own" in me["perms"] and "overview" not in me["perms"]
    for path in ("/api/v1/superadmin/overview", "/api/v1/superadmin/tenants", f"{API}/export.xlsx",
                 "/api/v1/superadmin/console/agents", "/api/v1/superadmin/team"):
        assert (await client.get(path, headers=h)).status_code == 403, path
    for path, body in ((f"{API}/today/limit", {"limit": 50}), (f"{API}/today/restaurants", {"enabled": True})):
        assert (await client.put(path, json=body, headers=h)).status_code == 403, path


def test_roles_and_naomy():
    assert sa_roles.ROLES["AMBASSADOR"][0] == "Ambassadeur" and sa_roles.only_own_prospects(["AMBASSADOR"])
    assert not sa_roles.only_own_prospects(["AMBASSADOR", "SALES"])  # cumulé avec Commercial : voit tout

    class M:
        id, email, full_name, invite_token_hash, active = "x", "a@b.c", "Kofi", None, True
        roles = ["AMBASSADOR"]
    view = console_bridge.member_view(M())
    assert view["actif"] is False and "AMBASSADOR" not in view["roles"]  # rien chez Naomy
    M.roles = ["AMBASSADOR", "SALES"]
    assert console_bridge.member_view(M()) ["roles"] == ["SALES"] and console_bridge.member_view(M())["actif"] is True


def test_screen_gives_the_ambassador_his_prospection():
    page = (Path(__file__).resolve().parents[1] / "static" / "superadmin" / "index.html").read_text(encoding="utf-8")
    assert 'data-perm="prospects.act" onclick="openProspectForm()"' in page and 'data-perm="prospects.edit" onclick="exportProspects()"' in page
    assert 'showSection(can("overview") ? "overview" : "prospects")' in page
