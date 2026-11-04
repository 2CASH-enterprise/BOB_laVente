"""
Lot 104 — corrections des parcours relevées par Thérèse le 03/11 :
A. photos toujours en JPEG ou PNG (un .webp est accepté par l'API puis jamais livré) ;
B. compte rendu d'envoi de WhatsApp : un écran non livré repart aussitôt en plus simple (une fois) ;
C. Bob dit ce qu'il a compris quand il saute une question (« Nous avons 1 appartement à louer, à Rennes : ») ;
D1. confirmation d'un rendez-vous en un seul message (Déplacer, Annuler, la suite) ;
E. statut d'un bien (Disponible, Sous compromis, Vendu, Loué) à la place du stock.
"""
import io
import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import get_settings
from app.models.conversation import Message
from app.models.product import Product
from app.services import guided_flow as gf
from app.services import immo_flow as imf
from app.services import photo_formats as pf
from app.services import product_sheets
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot53_courtier import _headers
from app.tests.test_lot94_parcours_boutons import _WA, guided_on  # noqa: F401 — fixture
from app.tests.test_lot94_parcours_boutons import _tap as _wa_tap
from app.tests.test_lot97_parcours_agence import _agence, _bien, _client, _go, _titles

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _guided(monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)


def _image(kind="WEBP", mode="RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (40, 30), (200, 120, 40) if mode == "RGB" else (200, 120, 40, 120)).save(out, kind)
    return out.getvalue()


# --- A. Photos ------------------------------------------------------------------------------------------------------

def test_photos_become_jpeg_or_png():
    content, ext = pf.sendable(_image("WEBP"), "webp")
    assert ext == "jpg" and Image.open(io.BytesIO(content)).format == "JPEG"
    content, ext = pf.sendable(_image("WEBP", "RGBA"), ".WEBP")  # transparence posée sur du blanc
    assert ext == "jpg" and Image.open(io.BytesIO(content)).mode == "RGB"
    clear = io.BytesIO()
    Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(clear, "WEBP", lossless=True)
    pixel = Image.open(io.BytesIO(pf.sendable(clear.getvalue(), "webp")[0])).getpixel((4, 4))
    assert min(pixel) > 240  # le transparent devient blanc, pas noir
    png = _image("PNG")
    assert pf.sendable(png, "png") == (png, "png") and pf.sendable(b"x", "jpeg") == (b"x", "jpg")


def test_a_webp_address_never_goes_out_as_a_photo():
    from app.agents.tools import has_sendable_photo

    for url, ok in (("https://x.fr/a.jpg", True), ("https://x.fr/a.PNG?v=2", True), ("https://x.fr/photo", True),
                    ("https://x.fr/a.webp", False), ("https://x.fr/a.WEBP?x=1", False), ("https://x.fr/a.gif", False),
                    ("http://x.fr/a.jpg", False)):
        assert has_sendable_photo(Product(image_url=url)) is ok, url
    assert pf.suffix_of("https://x.fr/dossier.v2/photo") == ""


@pytest.mark.asyncio
async def test_an_uploaded_webp_is_stored_as_jpeg(client, db_session, unique_email):
    tenant, items = await _agence(db_session, unique_email)
    r = await client.post(f"/api/v1/products/{items[0].id}/image", headers=await _headers(client, tenant),
                          files={"file": ("maison.webp", _image("WEBP"), "image/webp")})
    assert r.status_code == 200, r.text
    url = r.json()["image_url"]
    assert url.endswith(".jpg")
    stored = pf.UPLOADS / url.rsplit("/", 1)[-1]
    try:
        assert Image.open(stored).format == "JPEG"
    finally:
        stored.unlink(missing_ok=True)
    bad = await client.post(f"/api/v1/products/{items[0].id}/image", headers=await _headers(client, tenant),
                            files={"file": ("faux.webp", b"pas une image", "image/webp")})
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_existing_webp_photos_are_converted_once(db_session, unique_email, tmp_path):
    _, items = await _agence(db_session, unique_email)
    (tmp_path / "abc123.webp").write_bytes(_image("WEBP"))
    base = get_settings().public_base_url
    items[0].image_url = items[0].image_original_url = f"{base}/uploads/products/abc123.webp"
    items[1].image_url = f"{base}/uploads/products/absent.webp"  # fichier disparu : laissé, il partira sans photo
    items[2].image_url = "https://autre-site.fr/photo.webp"      # photo d'un autre site : laissée (sans photo)
    await db_session.commit()
    assert await pf.convert_existing(db_session, tmp_path) == 2
    assert items[0].image_url == items[0].image_original_url == f"{base}/uploads/products/abc123.jpg"
    assert Image.open(tmp_path / "abc123.jpg").format == "JPEG"
    assert items[1].image_url.endswith("absent.webp") and items[2].image_url == "https://autre-site.fr/photo.webp"
    assert await pf.convert_existing(db_session, tmp_path) == 0  # rejouable


# --- B. Un écran refusé ou non livré repart en plus simple ----------------------------------------------------------

def test_each_screen_has_a_simpler_version():
    cards = {"type": "carousel", "body": "Voici nos biens.", "cards": [
        {"link": "https://x/a.jpg", "body": "T3\n68 m² · Lyon\n*1 150 EUR* / mois", "name": "T3 lumineux avec balcon",
         "product_id": "1", "buttons": [("g:d:1", "Voir détails"), ("g:vi:1", "Demander une visite")]}]}
    listing = gf.degraded(cards)
    assert listing == {"type": "list", "body": "Voici nos biens.", "button": "Voir la liste",
                       "rows": [("g:d:1", "T3 lumineux avec balcon", "68 m² · Lyon · *1 150 EUR* / mois")]}
    photo = {"type": "buttons", "body": "Fiche", "buttons": [("g:vi:1", "Visite")], "image": "https://x/a.jpg", "product_id": "1"}
    assert gf.degraded(photo) == {"type": "buttons", "body": "Fiche", "buttons": [("g:vi:1", "Visite")], "product_id": "1"}
    assert gf.degraded(gf.degraded(photo)) == {"type": "text", "body": "Fiche"}
    assert gf.degraded(listing)["type"] == "text" and gf.degraded({"type": "text", "body": "x"}) is None


class _Meta(_WA):
    """Comme _WA, mais l'API répond avec l'identifiant du message (wamid), comme Meta."""

    async def _log(self, kind, **kw):
        await super()._log(kind, **kw)
        return {"messages": [{"id": f"wamid.{len(_WA.sent)}"}]}


@pytest.mark.asyncio
async def test_a_refused_screen_keeps_its_buttons_without_the_photo(db_session, unique_email):
    tenant, items = await _agence(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)

    class Refuses(_Meta):
        async def send_buttons(self, to, body, buttons, image_link=None):
            if image_link:
                raise RuntimeError("photo refusée")
            return await super().send_buttons(to, body, buttons)

    _WA.sent = []
    turn = await _go(db_session, tenant, customer, conv, f"g:d:{items[0].id}")
    assert await gf.deliver(db_session, tenant, conv, Refuses(), customer.whatsapp_number, turn) == 1
    assert _WA.sent[-1]["kind"] == "buttons" and _WA.sent[-1]["image"] is None and len(_WA.sent[-1]["buttons"]) == 3


@pytest.mark.asyncio
async def test_a_screen_whatsapp_could_not_deliver_is_sent_again_simpler(client, db_session, unique_email, guided_on,  # noqa: F811
                                                                          monkeypatch):
    await _agence(db_session, unique_email, pnid="pn-l104-1")
    guided_on({"intents": [], "objections": []}, [])
    _WA.sent = []
    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Meta)
    r = await client.post("/webhooks/whatsapp", json=_wa_tap("pn-l104-1", "33611110401", "g:il:tL:0", "Louer"))
    assert r.json()["guided"] is True
    trace = (await db_session.execute(select(Message).where(Message.message_type == "product_cards"))).scalar_one()
    wamid = trace.message_metadata["wamid"]
    assert trace.message_metadata["fallback"]["type"] == "list"
    before = len(_WA.sent)

    def failed(status="failed"):
        return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "pn-l104-1"}, "statuses": [
            {"id": wamid, "status": status, "recipient_id": "33611110401", "errors": [{"code": 131053}]}]}}]}]}

    assert (await client.post("/webhooks/whatsapp", json=failed("delivered"))).json() == {"status": "ignored"}
    r = await client.post("/webhooks/whatsapp", json=failed())
    assert r.json() == {"status": "delivery_failures", "resent": 1}
    assert _WA.sent[-1]["kind"] == "list" and len(_WA.sent) == before + 1
    assert [row[0].split(":")[1] for row in _WA.sent[-1]["rows"]] == ["d"] * len(_WA.sent[-1]["rows"])
    assert (await client.post("/webhooks/whatsapp", json=failed())).json()["resent"] == 0  # une seule fois
    other = failed()
    other["entry"][0]["changes"][0]["value"]["statuses"][0]["id"] = "wamid.inconnu"
    assert (await client.post("/webhooks/whatsapp", json=other)).json()["resent"] == 0


def test_failures_are_read_from_the_status_report():
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "pn"}, "statuses": [
        {"id": "w1", "status": "failed", "recipient_id": "336", "errors": [{"code": 131053, "title": "Media upload error"}]},
        {"id": "w2", "status": "read"}]}}]}]}
    assert gf.parse_failures(payload) == [{"phone_number_id": "pn", "wamid": "w1", "to": "336", "code": 131053}]
    assert gf.parse_failures({"entry": [{"changes": [{"value": {"messages": []}}]}]}) == []
    assert gf.message_id({"messages": [{"id": "wamid.X"}]}) == "wamid.X" and gf.message_id({}) is None


# --- C. Ce que Bob a compris ----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bob_says_why_a_question_was_skipped(db_session, unique_email):
    tenant, items = await _agence(db_session, unique_email, biens=lambda t: [
        _bien(t, "T3, Rennes", 900, city="Rennes", district=None),
        _bien(t, "T4, Lyon", 420000, tx="Vente", code="MAISON", city="Lyon", district=None),
        _bien(t, "Pavillon T4, Lyon", 520000, tx="Vente", code="MAISON", city="Lyon", district=None)])
    customer, conv = await _client(db_session, tenant)
    [card] = (await _go(db_session, tenant, customer, conv, "g:pl")).actions
    assert card["body"].startswith("Nous avons 1 appartement à louer, à Rennes :\n*T3, Rennes*")
    actions = (await _go(db_session, tenant, customer, conv, "g:pa")).actions
    assert actions[0]["type"] == "carousel"
    assert actions[0]["body"].startswith("Nous avons 2 maisons à vendre, à Lyon : Touchez « Demander une visite »")
    assert imf.intro([items[0], items[1]], {}) == "Nous avons 2 biens :"


# --- D1. Confirmation en un seul message -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_estimation_is_confirmed_in_one_message(db_session, unique_email):
    tenant, _ = await _agence(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    await _go(db_session, tenant, customer, conv, "g:pv")
    await _go(db_session, tenant, customer, conv, "g:ot:V:MAISON")
    [slots] = (await _go(db_session, tenant, customer, conv, text="Villeurbanne, Cusset, 120 m²")).actions
    turn = await _go(db_session, tenant, customer, conv, slots["rows"][0][0])
    [confirmation] = turn.actions
    assert "estimation" in confirmation["body"] and _titles(confirmation) == ["Déplacer", "Annuler", "Voir d'autres biens"]


# --- E. Statut d'un bien ---------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_property_status_replaces_the_stock(client, db_session, unique_email):
    tenant, _ = await _agence(db_session, unique_email)
    headers = await _headers(client, tenant)
    r = await client.post("/api/v1/products", headers=headers, json={
        "sku": "T5-NANTES", "name": "T5, Nantes", "price": 480000, "currency": "EUR", "bob_category": "MAISON",
        "estate_status": "Disponible", "sheet": {"transaction": "Vente", "city": "Nantes", "fees": "4 %"}})
    assert r.status_code == 201, r.text
    product = r.json()
    assert product["stock_quantity"] == 1 and product["sheet"]["status"] == "Disponible"
    r = await client.put(f"/api/v1/products/{product['id']}", headers=headers, json={"estate_status": "sous compromis"})
    assert r.json()["stock_quantity"] == 0 and r.json()["sheet"]["status"] == "Sous compromis"
    r = await client.put(f"/api/v1/products/{product['id']}", headers=headers,  # la fiche réenregistrée garde le statut
                         json={"bob_category": "MAISON", "sheet": {"transaction": "Vente", "city": "Nantes", "fees": "5 %"}})
    assert r.json()["sheet"]["status"] == "Sous compromis" and r.json()["sheet"]["fees"] == "5 %"
    assert (await client.put(f"/api/v1/products/{product['id']}", headers=headers, json={"estate_status": "Réservé"})).status_code == 422
    customer, conv = await _client(db_session, tenant)
    turn = await _go(db_session, tenant, customer, conv, "g:pa")  # seuls les biens disponibles sont proposés
    assert "T5, Nantes" not in str(turn.actions)
    r = await client.put(f"/api/v1/products/{product['id']}", headers=headers, json={"estate_status": "Disponible"})
    assert r.json()["stock_quantity"] == 1


def test_status_shown_for_older_properties():
    assert product_sheets.estate_status(Product(stock_quantity=1, sheet={})) == "Disponible"
    assert product_sheets.estate_status(Product(stock_quantity=0, sheet={"transaction": "Location"})) == "Loué"
    assert product_sheets.estate_status(Product(stock_quantity=0, sheet=None)) == "Vendu"
    assert product_sheets.estate_status(Product(stock_quantity=0, sheet={"status": "Sous compromis"})) == "Sous compromis"
    assert product_sheets.clean_estate_status("loue") == "Loué" and product_sheets.clean_estate_status("x") is None


def test_dashboard():
    assert 'id="new-product-estate-status"' in HTML and "edit-estate-status-${p.id}" in HTML
    assert 'class="input no-insurance no-estate no-restaurant"' in HTML and "function estateStatusOf(" in HTML
    assert 'payload.estate_status = document.getElementById("new-product-estate-status").value' in HTML
    assert "estate_status: document.getElementById(`edit-estate-status-${productId}`).value" in HTML
    assert uuid
