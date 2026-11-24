"""
Lot 130 — prospection des restaurants par le diagnostic de Naomy (décisions du 23/11) : interrupteur éteint par
défaut (aucun restaurant dans la liste du jour, aucun e-mail), lien …/audit/CODE qui note le clic puis ouvre WhatsApp
chez Naomy (« audit P-CODE »), test A / B du premier message, code du prospect rendu par Naomy à la création du compte
et avancée dans le diagnostic (contrat 1.7.3), textes et visuel aux couleurs d'AgenC'AI.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.prospect import Prospect, ProspectCampaign, ProspectEvent
from app.services import prospect_mailer as mailer
from app.services import prospection as pros
from app.tests.test_lot89_connecteur_naomy import _signed, naomy_env  # noqa: F401 — fixture
from app.tests.test_lot118_equipe_naomy import _create
from app.tests.test_lot119_roles_super_admin import _h, _member

API = "/api/v1/superadmin/prospects"
BROWSER = {"user-agent": "Mozilla/5.0 (Linux; Android 14) Chrome/130"}
ROOT = Path(__file__).resolve().parents[1]


def _resto(code, **over):
    values = dict(company="Maquis Chez Awa", contact_name="Awa Koné", sector="RESTAURANT", code=code, country="CI",
                  phone="2250701000001", status="ACTIVE")
    values.update(over)
    return Prospect(**values)


def test_half_of_the_codes_get_each_version_and_always_the_same():
    codes = [f"c{i:05d}" for i in range(200)]
    versions = [pros.variant(_resto(c)) for c in codes]
    assert 70 < versions.count("A") < 130 and set(versions) == {"A", "B"}
    assert versions == [pros.variant(_resto(c)) for c in codes]


def test_both_first_messages_and_the_relance():
    a = next(_resto(f"a{i}") for i in range(50) if pros.variant(_resto(f"a{i}")) == "A")
    b = next(_resto(f"b{i}") for i in range(50) if pros.variant(_resto(f"b{i}")) == "B")
    text_a, text_b = pros.message(a, "Thérèse"), pros.message(b, "Thérèse")
    assert text_a.startswith("Awa, une question rapide 👋\n\nHier soir, combien de clients ont écrit à *Maquis Chez Awa*")
    assert text_b.startswith("Awa, et si *Maquis Chez Awa* ne ratait plus aucune commande WhatsApp ? 👀")
    for text, code in ((text_a, a.code), (text_b, b.code)):
        assert "Je suis Thérèse, d'AgenC'AI." in text and "Nous installons tout" in text
        assert text.endswith(f"👉 Votre *diagnostic gratuit*, en 2 minutes sur WhatsApp :\n{get_settings().public_base_url.rstrip('/')}"
                             f"/audit/{code}\n\nRépondez STOP pour ne plus être contacté.")
        assert "FCFA" not in text and "démo" not in text
    a.contact_name = None  # sans prénom : la phrase commence par une majuscule
    assert pros.message(a, None).startswith("Une question rapide 👋") and "Je vous écris de la part d'AgenC'AI." in pros.message(a, None)
    b.contact_name = None
    assert pros.message(b, None).startswith("Et si *Maquis Chez Awa*")
    with_offer = pros.message(a, "Thérèse", "Les 100 premiers inscrits ont l'installation offerte.")
    assert with_offer.split("\n")[-3] == "Les 100 premiers inscrits ont l'installation offerte."
    relance = pros.relance_message(_resto("r1"))
    assert relance.startswith("Awa, votre diagnostic gratuit pour *Maquis Chez Awa* est toujours prêt 👇")
    assert "/audit/r1" in relance and relance.endswith("Je ne vous relancerai plus. Répondez STOP pour ne plus être contacté.")
    shop = Prospect(company="Boutique", sector="ONLINE_STORE", code="s1", country="CI")  # les autres secteurs ne changent pas
    assert "secteur=commerce" in pros.message(shop, "Noé") and pros.link("s1", "ONLINE_STORE").endswith("p=s1")


def test_emails_and_visual():
    p = _resto("e1", email="awa@exemple.ci")
    subjects = [mailer.render(p, s, b)[0] for s, b in mailer.DEFAULTS["RESTAURANT"]]
    assert subjects == ["Maquis Chez Awa : la commande que vous avez ratée hier soir", "Votre plat du jour mérite mieux",
                        "Je ferme votre dossier ?"]
    for s, b in mailer.DEFAULTS["RESTAURANT"]:
        _, text, html = mailer.render(p, s, b)
        assert "/audit/e1" in text and "Faire mon diagnostic gratuit" in html and "FCFA" not in text
    visual = ROOT / "static" / "visuels" / "prospection-restaurant.jpg"
    assert 40_000 < visual.stat().st_size < 300_000  # WhatsApp n'affiche pas les aperçus trop lourds


@pytest.mark.asyncio
async def test_the_audit_link_counts_the_click_and_opens_naomy(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "naomy_whatsapp_number", "+33 7 80 84 12 69")
    monkeypatch.setattr(get_settings(), "naomy_whatsapp_numbers", "")  # le numéro par pays : test plus bas
    db_session.add(_resto("k7p2qx"))
    await db_session.commit()
    bot = await client.get("/audit/k7p2qx", headers={"user-agent": "WhatsApp/2.23"})
    assert bot.status_code == 200 and "prospection-restaurant.jpg" in bot.text and "Diagnostic gratuit" in bot.text
    r = await client.get("/audit/K7P2QX", headers=BROWSER)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and "bob_p" in r.headers.get("set-cookie", "")
    assert 'content="0;url=https://wa.me/33780841269?text=audit%20P-K7P2QX"' in r.text and "noindex" in r.text
    p = (await db_session.execute(select(Prospect).where(Prospect.code == "k7p2qx")
                                  .execution_options(populate_existing=True))).scalar_one()
    assert p.click_count == 1 and p.first_click_at is not None  # le robot d'aperçu ne compte pas
    unknown = await client.get("/audit/zzzzzz", headers=BROWSER)
    assert "text=audit\"" in unknown.text or unquote("text=audit") in unknown.text and "P-" not in unknown.text.split("wa.me")[1][:40]
    monkeypatch.setattr(get_settings(), "naomy_whatsapp_number", "")
    fallback = await client.get("/audit/k7p2qx", headers=BROWSER, follow_redirects=False)
    assert fallback.status_code == 302 and "secteur=restaurant" in fallback.headers["location"]


@pytest.mark.asyncio
async def test_the_switch_holds_restaurants_back_until_launch(client, db_session):
    headers = _h(await _member(db_session, "SALES", name="Noé Bakouba"))
    db_session.add_all([_resto("t1"), Prospect(company="Boutique", sector="ONLINE_STORE", code="t2", country="CI",
                                               phone="2250701000002", status="ACTIVE")])
    await db_session.commit()
    paused = (await client.get(f"{API}/today", headers=headers)).json()
    assert [r["company"] for r in paused["to_send"]] == ["Boutique"] and paused["restaurants"]["enabled"] is False
    viewer = _h(await _member(db_session, "READONLY", name="Lecteur"))
    assert (await client.put(f"{API}/today/restaurants", json={"enabled": True}, headers=viewer)).status_code == 403
    on = (await client.put(f"{API}/today/restaurants", json={"enabled": True}, headers=headers)).json()
    assert on["restaurants"]["enabled"] is True and on["restaurants"]["changed_by"] == "Noé Bakouba"
    row = next(r for r in on["to_send"] if r["company"] == "Maquis Chez Awa")
    assert row["variant"] in ("A", "B") and "/audit/t1" in row["message"] and "audit" in row["whatsapp_url"]
    assert [v["variant"] for v in on["restaurants"]["ab"]] == ["A", "B"]


@pytest.mark.asyncio
async def test_no_email_to_restaurants_while_paused(db_session):
    campaign = ProspectCampaign(name="Restaurants", sector="RESTAURANT", status="RUNNING",
                                subject_1=mailer.DEFAULTS["RESTAURANT"][0][0], body_1=mailer.DEFAULTS["RESTAURANT"][0][1])
    db_session.add(campaign)
    await db_session.flush()
    db_session.add(_resto("m1", email="awa@exemple.ci", campaign_id=campaign.id))
    await db_session.commit()
    monday_10h_utc = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    assert (await mailer.next_candidate(db_session, monday_10h_utc))[0] is None
    (await mailer.get_settings_row(db_session)).restaurants_enabled = True
    await db_session.commit()
    assert (await mailer.next_candidate(db_session, monday_10h_utc))[0].code == "m1"


@pytest.mark.asyncio
async def test_naomy_gives_back_the_prospect_code(client, db_session, naomy_env):
    db_session.add(_resto("k7p2qx", email="autre@exemple.ci"))
    await db_session.commit()
    r = await _create(client, code_prospect="P-K7P2QX")
    assert r.status_code == 201, r.text
    p = (await db_session.execute(select(Prospect).where(Prospect.code == "k7p2qx")
                                  .execution_options(populate_existing=True))).scalar_one()
    assert p.signed_up_at is not None and str(p.tenant_id) == r.json()["boutique_id"]
    other = await _create(client, code_prospect="n'importe quoi", numero_whatsapp="+2250701029999", email="b@maquis.ci")
    assert other.status_code == 201  # code mal formé : ignoré, jamais une erreur


@pytest.mark.asyncio
async def test_naomy_reports_the_audit_steps(client, db_session, naomy_env):
    db_session.add(_resto("k7p2qx"))
    await db_session.commit()

    async def step(body, secret=None):
        raw, headers = _signed(body) if secret is None else _signed(body, secret=secret)
        return await client.post("/api/v1/naomy/prospection/parcours", content=raw, headers=headers)

    assert (await step({"code_prospect": "P-K7P2QX", "etape": "audit_demarre"})).json() == {"pris_en_compte": True}
    for _ in range(2):  # renvoyé : une seule trace
        assert (await step({"code_prospect": "p-k7p2qx", "etape": "audit_termine"})).json() == {"pris_en_compte": True}
    p = (await db_session.execute(select(Prospect).where(Prospect.code == "k7p2qx")
                                  .execution_options(populate_existing=True))).scalar_one()
    assert p.demo_at is not None
    details = [e.detail for e in (await db_session.execute(select(ProspectEvent).where(ProspectEvent.prospect_id == p.id)
                                                           .order_by(ProspectEvent.at))).scalars()]
    assert details.count("Diagnostic Naomy terminé") == 1 and "Diagnostic Naomy commencé" in details
    assert (await step({"code_prospect": "P-ZZZZZZ", "etape": "audit_termine"})).json() == {"pris_en_compte": False}
    assert (await step({"code_prospect": "P-K7P2QX", "etape": "inscrit"})).status_code == 400
    assert (await step({"code_prospect": "P-K7P2QX", "etape": "audit_termine"}, secret="faux")).status_code == 401


@pytest.mark.asyncio
async def test_ab_stats(db_session):
    now = datetime.now(timezone.utc)
    codes = [f"s{i:03d}" for i in range(40)]
    for code in codes:
        db_session.add(_resto(code, contacted_at=now - timedelta(days=3), first_click_at=now if code.endswith("0") else None))
    await db_session.commit()
    stats = await pros.ab_stats(db_session)
    assert sum(v["contacted"] for v in stats) == 40 and sum(v["clicked"] for v in stats) == 4
    by = {v["variant"]: v["contacted"] for v in stats}
    assert by["A"] == sum(1 for c in codes if pros.variant(_resto(c)) == "A")


def test_super_admin_page_has_the_switch_and_the_table():
    page = (ROOT / "static" / "superadmin" / "index.html").read_text(encoding="utf-8")
    assert "Lancer la prospection des restaurants" in page and "/api/v1/superadmin/prospects/today/restaurants" in page
    assert "<th>Diagnostic</th>" in page


@pytest.mark.asyncio
async def test_each_country_gets_its_agencai_number(client, db_session, monkeypatch):
    """Lot 130 b — Côte d'Ivoire : +225 (Naomy, lot 9g) ; autres pays : +33."""
    monkeypatch.setattr(get_settings(), "naomy_whatsapp_number", "+33 7 80 84 12 69")
    monkeypatch.setattr(get_settings(), "naomy_whatsapp_numbers", "CI=+225 05 09 77 73 33, SN=")
    db_session.add_all([_resto("ci2345"), _resto("cm2345", country="CM"), _resto("sn2345", country="SN")])
    await db_session.commit()
    ci = await client.get("/audit/ci2345", headers=BROWSER)
    assert "https://wa.me/2250509777333?text=audit%20P-CI2345" in ci.text
    for code in ("cm2345", "sn2345"):  # Cameroun sur le +33 ; pays réglé sans numéro : le +33 aussi
        assert "https://wa.me/33780841269?text=audit%20P-" + code.upper() in (await client.get(f"/audit/{code}", headers=BROWSER)).text
    assert pros.naomy_number_for(None) == "33780841269" and pros.naomy_number_for("ci") == "2250509777333"
