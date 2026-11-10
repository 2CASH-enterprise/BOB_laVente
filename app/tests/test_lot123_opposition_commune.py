"""
Lot 123 — liste d'opposition commune et dédoublonnage Bob ↔ Naomy (contrat 1.6), par empreintes SHA-256 seulement,
signés par le secret de plateforme.
"""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.prospect import Prospect, ProspectSuppression
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import naomy_bridge as nb
from app.services import prospection as pros
from app.tests.test_lot89_connecteur_naomy import SECRET, _signed, conforms, naomy_env  # noqa: F401

API = "/api/v1/naomy/prospection"


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _get_headers(secret=SECRET):
    stamp = str(int(time.time()))
    return {"X-Platform-Timestamp": stamp, "X-Platform-Signature": nb.sign(secret, stamp, b"")}


async def _post(client, path, body, secret=SECRET):
    raw, headers = _signed(body, secret=secret)
    return await client.post(f"{API}{path}", content=raw, headers=headers)


@pytest.mark.asyncio
async def test_naomy_declares_oppositions_and_bob_respects_them(client, db_session, naomy_env):
    body = {"oppositions": [{"empreinte": h("awa@exemple.ci"), "type": "email", "raison": "stop"},
                            {"empreinte": h("2250701000031"), "type": "telephone", "raison": "plainte"}]}
    r = await _post(client, "/oppositions", body)
    assert r.status_code == 200 and r.json() == {"ajoutees": 2, "deja_connues": 0}
    assert (await _post(client, "/oppositions", body)).json() == {"ajoutees": 0, "deja_connues": 2}
    rows = (await db_session.execute(select(ProspectSuppression))).scalars().all()
    assert {(r.kind, r.reason, r.origin) for r in rows} == {("email", "UNSUBSCRIBED", "NAOMY"), ("telephone", "COMPLAINT", "NAOMY")}
    # Aussitôt chez Bob : jamais dans la liste du jour, jamais d'e-mail de prospection.
    blocked = await pros.blocked_contacts(db_session)
    assert pros.is_blocked(blocked, "Awa@Exemple.ci", None) and pros.is_blocked(blocked, None, "+225 07 01 00 00 31")
    from app.services import prospect_mailer

    assert await prospect_mailer.suppressed(db_session, "AWA@exemple.ci")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"oppositions": []}, {"oppositions": [{"empreinte": "abc", "type": "email", "raison": "stop"}]},
                                  {"oppositions": [{"empreinte": h("x"), "type": "fax", "raison": "stop"}]},
                                  {"oppositions": [{"empreinte": h("x"), "type": "email", "raison": "lassitude"}]},
                                  {"oppositions": [{"empreinte": h("x").upper(), "type": "email", "raison": "stop"}]},
                                  {"oppositions": [{"empreinte": h("x"), "type": "email", "raison": "stop"}], "en_trop": 1},
                                  {"oppositions": [{"empreinte": h(str(i)), "type": "email", "raison": "stop"} for i in range(1001)]}])
async def test_bad_bodies(client, db_session, naomy_env, body):
    assert (await _post(client, "/oppositions", body)).status_code == 400
    assert not (await db_session.execute(select(ProspectSuppression))).scalars().all()


@pytest.mark.asyncio
async def test_signature_is_required(client, db_session, naomy_env):
    body = {"empreintes": [{"empreinte": h("a@b.ci"), "type": "email"}]}
    assert (await _post(client, "/verifier", body, secret="autre")).status_code == 401
    assert (await client.get(f"{API}/oppositions", headers=_get_headers("autre"))).status_code == 401
    assert (await client.get(f"{API}/oppositions")).status_code == 401


@pytest.mark.asyncio
async def test_naomy_reads_all_oppositions_page_by_page(client, db_session, naomy_env):
    base = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    for i in range(5):
        db_session.add(ProspectSuppression(email_hash=h(f"p{i}@x.ci"), kind="email", reason="BOUNCED" if i == 0 else "UNSUBSCRIBED",
                                           origin="BOB", created_at=base + timedelta(minutes=i // 2)))  # dates égales deux à deux
    await db_session.commit()
    seen, cursor = [], None
    for _ in range(4):
        r = await client.get(f"{API}/oppositions", params={"limite": 2, **({"depuis": cursor} if cursor else {})}, headers=_get_headers())
        assert r.status_code == 200
        data = r.json()
        for item in data["oppositions"]:
            conforms(item, "Opposition")
        seen += [o["empreinte"] for o in data["oppositions"]]
        cursor = data["curseur_suivant"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(h(f"p{i}@x.ci") for i in range(5)) and len(seen) == 5
    first = (await client.get(f"{API}/oppositions", headers=_get_headers())).json()["oppositions"]
    assert {o["raison"] for o in first} == {"invalide", "desinscrit"} and {o["origine"] for o in first} == {"bob"}
    assert (await client.get(f"{API}/oppositions", params={"depuis": "!!!"}, headers=_get_headers())).status_code == 400


@pytest.mark.asyncio
async def test_verify_tells_what_to_exclude_and_why(client, db_session, naomy_env):
    now = datetime.now(timezone.utc)
    shop = Tenant(name="Client", country="CI", currency="XOF", email="client@bob.ci", phone="07 01 00 00 41", plan=TenantPlan.PRO)
    demo = Tenant(name="Démo", country="CI", currency="XOF", email="demo@bob.ci", plan=TenantPlan.FREE, is_demo=True)
    db_session.add_all([shop, demo])
    await db_session.flush()
    db_session.add_all([
        User(tenant_id=shop.id, email="gerant@bob.ci", full_name="G", role=Role.OWNER, hashed_password="x"),
        WhatsAppAccount(tenant_id=shop.id, phone_number_id="pn-123", waba_id="w", display_phone_number="+225 07 01 00 00 42", system_user_token="t"),
        ProspectSuppression(email_hash=h("stop@x.ci"), kind="email", reason="UNSUBSCRIBED"),
        Prospect(company="En cours", code="c1", phone="2250701000043", sector="RESTAURANT", country="CI", status="ACTIVE"),
        Prospect(company="Non récent", code="c2", email="non@x.ci", sector="RESTAURANT", country="CI", status="NOT_INTERESTED",
                 updated_at=now - timedelta(days=10)),
        Prospect(company="Non ancien", code="c3", email="vieux@x.ci", sector="RESTAURANT", country="CI", status="NOT_INTERESTED",
                 updated_at=now - timedelta(days=400), created_at=now - timedelta(days=400)),
        Prospect(company="Désinscrit", code="c4", phone="2250701000044", sector="RESTAURANT", country="CI", status="UNSUBSCRIBED"),
        Prospect(company="Adresse morte", code="c5", email="recent@x.ci", sector="RESTAURANT", country="CI", status="INVALID"),
    ])
    await db_session.commit()
    ask = ["stop@x.ci", "client@bob.ci", "gerant@bob.ci", "2250701000041", "2250701000042", "2250701000043", "non@x.ci",
           "vieux@x.ci", "2250701000044", "demo@bob.ci", "libre@x.ci"]
    body = {"empreintes": [{"empreinte": h(v), "type": "email" if "@" in v else "telephone"} for v in ask]}
    r = await _post(client, "/verifier", body)
    assert r.status_code == 200
    conforms(r.json(), "Exclusions")
    motives = {e["empreinte"]: e["motif"] for e in r.json()["exclure"]}
    assert motives == {h("stop@x.ci"): "opposition", h("client@bob.ci"): "client_bob", h("gerant@bob.ci"): "client_bob",
                       h("2250701000041"): "client_bob", h("2250701000042"): "client_bob", h("2250701000043"): "prospect_bob",
                       h("non@x.ci"): "pas_interesse", h("2250701000044"): "opposition"}
    # Coordonnées invalides : opposition.
    r = await _post(client, "/verifier", {"empreintes": [{"empreinte": h("recent@x.ci"), "type": "email"}]})
    assert r.json()["exclure"] == [{"empreinte": h("recent@x.ci"), "motif": "opposition"}]


@pytest.mark.asyncio
async def test_stop_at_bob_is_seen_by_naomy(client, db_session, naomy_env):
    from app.tests.test_lot119_roles_super_admin import _h, _member

    headers = _h(await _member(db_session, "SALES"))
    r = await client.post("/api/v1/superadmin/prospects", headers=headers,
                          json={"company": "Maquis", "phone": "0701000050", "email": "m@x.ci", "sector": "RESTAURANT", "country": "CI"})
    await client.post(f"/api/v1/superadmin/prospects/{r.json()['id']}/status", headers=headers, json={"status": "UNSUBSCRIBED"})
    items = (await client.get(f"{API}/oppositions", headers=_get_headers())).json()["oppositions"]
    assert {(o["empreinte"], o["type"], o["origine"]) for o in items} == {(h("m@x.ci"), "email", "bob"), (h("2250701000050"), "telephone", "bob")}


def test_the_contract_lists_the_hash_rules():
    from app.tests.test_lot89_connecteur_naomy import _contract

    doc = _contract()
    assert "x-prospection" in doc and "sha256(\"2250701020304\")" in doc["x-prospection"]["empreintes"]
    assert "prospectionVerifier" in doc["webhooks"]
    assert json.dumps(doc["components"]["schemas"]["Exclusions"]).count("prospect_bob") == 1


@pytest.mark.asyncio
async def test_rate_limit(client, db_session, naomy_env):
    for _ in range(120):
        assert (await client.get(f"{API}/oppositions", headers=_get_headers())).status_code == 200
    r = await client.get(f"{API}/oppositions", headers=_get_headers())
    assert r.status_code == 429 and r.json()["erreur"] == "trop_de_requetes"
