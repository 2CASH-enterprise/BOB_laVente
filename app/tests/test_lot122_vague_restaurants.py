"""
Lot 122 — vague « restaurants CI + CM de l'ancienne Naomy » dans la prospection de Bob : colonnes Groupe (ordre de
passage) et Relation passée (phrase sur Naomy), textes plus vendeurs (WhatsApp *gras*, e-mail **gras**), et jamais un
contact opposé ni un commerçant déjà client (à l'import et dans la liste du jour). « Ne plus contacter » met l'e-mail
ET le téléphone dans la liste d'opposition (empreintes SHA-256).
"""
import hashlib
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.prospect import Prospect, ProspectSuppression
from app.models.tenant import Tenant, TenantPlan
from app.models.whatsapp_account import WhatsAppAccount
from app.services import prospect_mailer as mailer
from app.services import prospection as pros
from app.tests.test_lot119_roles_super_admin import _h, _member

API = "/api/v1/superadmin/prospects"
CSV_HEAD = "Entreprise;Contact;Téléphone;Email;Ville;Pays;Secteur;Source;Groupe;Relation passée\n"


def _csv(*rows) -> bytes:
    return (CSV_HEAD + "\n".join(";".join(r) for r in rows) + "\n").encode("utf-8")


async def _import(client, headers, raw):
    files = {"file": ("vague.csv", raw, "text/csv")}
    preview = (await client.post(f"{API}/import/preview", headers=headers, files=files)).json()
    report = (await client.post(f"{API}/import", headers=headers, files=files, data={
        "mapping": json.dumps(preview["mapping"]), "country": "CI", "sector": "RESTAURANT", "source": ""})).json()
    return preview, report


def test_restaurant_whatsapp_texts():
    p = Prospect(company="Maquis Chez Awa", contact_name="Awa Koné", sector="RESTAURANT", code="r122", country="CI")
    first = pros.message(p, "Noé")
    assert first.startswith("Bonjour Awa 👋, je suis Noé d'AgenC'AI.\n\nEn plein service")
    assert "📣 *Naomy*, votre community manager intelligente" in first and "🍽️ *Bob*, votre serveur virtuel" in first
    assert "elle a bien changé" not in first
    lines = first.split("\n")
    assert lines[-3].startswith("👉 Voir la démo : https://") and lines[-1] == "Répondez STOP pour ne plus être contacté."
    p.past_relation = True
    again = pros.message(p, "Noé")
    assert again.startswith("Bonjour Awa 👋, je suis Noé d'AgenC'AI. Vous aviez échangé avec Naomy il y a quelques mois : "
                            "elle a bien changé depuis.\n\n")
    relance = pros.relance_message(p)
    assert relance.startswith("Bonjour Awa, un dernier mot pour Maquis Chez Awa : pendant que *Naomy*")
    assert relance.endswith("Je ne vous relancerai plus. Répondez STOP pour ne plus être contacté.")
    assert "FCFA" not in first + relance
    shop = Prospect(company="Boutique", sector="ONLINE_STORE", code="s122", country="CI")  # les autres secteurs ne changent pas
    assert pros.message(shop, "Noé").startswith("Bonjour, je suis Noé d'AgenC'AI. Bob, notre vendeur virtuel")
    assert "Je ne vous relancerai plus" not in pros.relance_message(shop)


def test_restaurant_email_has_bold_names(monkeypatch):
    p = Prospect(company="Maquis Chez Awa", contact_name="Awa Koné", sector="RESTAURANT", code="r122", country="CI",
                 email="awa@exemple.ci")
    monkeypatch.setattr(mailer, "stop_url", lambda prospect: "https://x/stop")
    subject, body = mailer.DEFAULTS["RESTAURANT"][0]
    subject, text, html = mailer.render(p, subject, body)
    assert subject == "Maquis Chez Awa : plus de clients, sans perdre une commande"
    assert "<strong>Naomy</strong>, votre community manager intelligente" in html and "<strong>Bob</strong>" in html
    assert "**" not in text and "• Naomy, votre community manager" in text and "**" not in html
    assert html.count("Voir la démo") == 1 and 'prospection-restaurant.jpg' in html
    for subject, body in mailer.DEFAULTS["RESTAURANT"][1:]:
        assert "**Naomy** et **Bob**" in body


@pytest.mark.asyncio
async def test_import_with_group_and_past_relation(client, db_session):
    headers = _h(await _member(db_session, "SALES", name="Noé Bakouba"))
    raw = _csv(["Maquis Trois", "Ali", "+2250701000003", "", "Abidjan", "CI", "Restaurant", "ancienne Naomy", "3", "non"],
               ["Maquis Un", "Awa Koné", "+2250701000001", "", "Abidjan", "CI", "Restaurant", "ancienne Naomy", "1", "oui"],
               ["Grill Douala", "", "+237670000002", "", "Douala", "CM", "Restaurant", "ancienne Naomy", "2", "OUI"],
               ["Maquis Un bis", "", "+2250701000004", "", "", "CI", "Restaurant", "ancienne Naomy", "1.0", ""],
               ["Mauvais groupe", "", "+2250701000005", "", "", "CI", "Restaurant", "", "premier", "non"],
               ["Mauvaise relation", "", "+2250701000006", "", "", "CI", "Restaurant", "", "1", "peut-être"],
               ["Groupe zéro", "", "+2250701000007", "", "", "CI", "Restaurant", "", "0", "non"],
               ["Groupe énorme", "", "+2250701000008", "", "", "CI", "Restaurant", "", "150", "non"])
    preview, report = await _import(client, headers, raw)
    assert preview["mapping"][-2:] == ["group", "past_relation"]
    assert {f["value"] for f in preview["fields"]} >= {"group", "past_relation"}
    assert report["created"] == 4 and [e["line"] for e in report["errors"]] == [6, 7, 8, 9]
    rows = {p.company: p for p in (await db_session.execute(select(Prospect))).scalars().all()}
    assert (rows["Maquis Un"].priority, rows["Maquis Un"].past_relation) == (1, True)
    assert (rows["Grill Douala"].priority, rows["Grill Douala"].past_relation, rows["Grill Douala"].country) == (2, True, "CM")
    assert (rows["Maquis Un bis"].priority, rows["Maquis Un bis"].past_relation) == (1, False)
    assert rows["Maquis Trois"].source == "ancienne Naomy" and rows["Maquis Trois"].sector == "RESTAURANT"
    detail = (await client.get(f"{API}/{rows['Maquis Un'].id}", headers=headers)).json()
    assert detail["group"] == 1 and detail["past_relation"] is True and "elle a bien changé" in detail["message"]
    # La liste du jour : groupe 1 (ordre du fichier), puis 2, puis 3, puis les prospects sans groupe.
    db_session.add(Prospect(company="Ancien prospect", phone="2250701000099", sector="ONLINE_STORE", code="old122",
                            country="CI", status="ACTIVE", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    await db_session.commit()
    today = (await client.get(f"{API}/today", headers=headers)).json()
    assert [r["company"] for r in today["to_send"]] == ["Maquis Un", "Maquis Un bis", "Grill Douala", "Maquis Trois", "Ancien prospect"]
    assert "*Naomy*" in today["to_send"][0]["message"] and "elle a bien changé" in today["to_send"][0]["message"]
    assert "elle a bien changé" not in today["to_send"][1]["message"]


@pytest.mark.asyncio
async def test_opposed_and_client_contacts_are_never_imported_nor_listed(client, db_session):
    headers = _h(await _member(db_session, "SALES"))
    shop = Tenant(name="Client Bob", country="CI", currency="XOF", email="client@bob.ci", phone="07 01 00 00 12", plan=TenantPlan.PRO)
    other = Tenant(name="Client WA", country="CI", currency="XOF", email="wa@bob.ci", plan=TenantPlan.PRO)
    db_session.add_all([shop, other])
    await db_session.flush()
    from app.models.user import Role, User

    db_session.add(User(tenant_id=shop.id, email="gerant@bob.ci", full_name="G", role=Role.OWNER, hashed_password="x"))
    db_session.add(WhatsAppAccount(tenant_id=other.id, phone_number_id="pn-122", waba_id="w", display_phone_number="+225 07 01 00 00 13",
                                   system_user_token="t"))
    db_session.add_all([ProspectSuppression(email_hash=hashlib.sha256(b"stop@exemple.ci").hexdigest(), reason="UNSUBSCRIBED"),
                        ProspectSuppression(email_hash=hashlib.sha256(b"2250701000014").hexdigest(), reason="UNSUBSCRIBED")])
    await db_session.commit()
    raw = _csv(["Opposé mail", "", "", "STOP@exemple.ci", "", "CI", "Restaurant", "", "1", "non"],
               ["Opposé tel", "", "+2250701000014", "", "", "CI", "Restaurant", "", "1", "non"],
               ["Déjà client", "", "+2250701000012", "", "", "CI", "Restaurant", "", "1", "non"],
               ["Déjà client WA", "", "+2250701000013", "", "", "CI", "Restaurant", "", "1", "non"],
               ["Déjà client mail", "", "", "Client@bob.ci", "", "CI", "Restaurant", "", "1", "non"],
               ["Compte d'un gérant", "", "", "gerant@bob.ci", "", "CI", "Restaurant", "", "1", "non"],
               ["Nouveau", "", "+2250701000015", "", "", "CI", "Restaurant", "", "1", "non"])
    _, report = await _import(client, headers, raw)
    assert report["created"] == 1 and report["excluded"] == 6
    # Un prospect déjà présent devenu client, ou opposé, sort aussi de la liste du jour.
    db_session.add(Prospect(company="Devenu client", phone="2250701000012", sector="RESTAURANT", code="cli122", country="CI", status="ACTIVE"))
    await db_session.commit()
    today = (await client.get(f"{API}/today", headers=headers)).json()
    assert [r["company"] for r in today["to_send"]] == ["Nouveau"]


@pytest.mark.asyncio
async def test_stop_puts_email_and_phone_in_the_opposition_list(client, db_session):
    headers = _h(await _member(db_session, "SALES"))
    r = await client.post(API, headers=headers, json={"company": "Maquis Stop", "phone": "0701000020", "email": "Stop2@Exemple.ci",
                                                      "sector": "RESTAURANT", "country": "CI"})
    pid = r.json()["id"]
    await client.post(f"{API}/{pid}/status", headers=headers, json={"status": "UNSUBSCRIBED", "reason": "STOP sur WhatsApp"})
    hashes = set((await db_session.execute(select(ProspectSuppression.email_hash))).scalars().all())
    assert hashlib.sha256(b"stop2@exemple.ci").hexdigest() in hashes and hashlib.sha256(b"2250701000020").hexdigest() in hashes
    await client.post(f"{API}/{pid}/status", headers=headers, json={"status": "UNSUBSCRIBED"})  # pas de doublon
    assert len((await db_session.execute(select(ProspectSuppression))).scalars().all()) == 2
    # Désinscription par l'e-mail : le téléphone suit aussi.
    p = Prospect(company="Par mail", phone="2250701000021", email="m@exemple.ci", sector="RESTAURANT", code="m122", country="CI", status="ACTIVE")
    db_session.add(p)
    await db_session.commit()
    await mailer.unsubscribe(db_session, p, "lien de l'e-mail")
    hashes = set((await db_session.execute(select(ProspectSuppression.email_hash))).scalars().all())
    assert hashlib.sha256(b"2250701000021").hexdigest() in hashes
    assert pros.contact_hash(phone="+225 07 01 00 00 21") == hashlib.sha256(b"2250701000021").hexdigest()
    assert pros.contact_hash(phone="12") is None and pros.contact_hash() is None
