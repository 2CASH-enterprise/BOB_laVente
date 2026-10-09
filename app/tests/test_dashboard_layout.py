"""
Organisation du tableau de bord (lot 17) : chaque carte dans l'onglet où le commerçant la cherche,
et chargée quand cet onglet s'ouvre.
"""
import re
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


def _section(tab: str) -> str:
    match = re.search(rf'<section id="tab-{tab}"[^>]*>(.*?)\n    </section>', HTML, re.S)
    assert match, f"onglet {tab} introuvable"
    return match.group(1)


def _show_tab_line(tab: str) -> str:
    body = re.search(r"function showTab\(name\) \{(.*?)\n\}", HTML, re.S).group(1)
    return next(line for line in body.splitlines() if f'name === "{tab}"' in line)


@pytest.mark.parametrize("tab, cards", [
    ("integrations", ["Compte WhatsApp Business", "Liens WhatsApp &amp; widgets", "Shopify", "Catalogue Facebook (Meta)"]),
    ("bob", ["Négociation de prix", "Transmission à un humain", "Réponses aux objections", "Relances par email"]),
    ("settings", ["Sécurité du compte"]),
    ("products", ["Ajouter un produit", "Importer un catalogue CSV", "Codes QR produits"]),
])
def test_each_card_is_in_its_tab(tab, cards):
    section = _section(tab)
    for card in cards:
        assert card in section, f"« {card} » devrait être dans l'onglet {tab}"


def test_moved_cards_left_their_old_tab():
    assert "Compte WhatsApp Business" not in _section("settings")
    assert "Négociation de prix" not in _section("settings")
    assert "Liens WhatsApp" not in _section("products")


def test_integrations_start_with_whatsapp_then_links():
    section = _section("integrations")
    order = [section.index(c) for c in ("Compte WhatsApp Business", "Liens WhatsApp", "Shopify", "Catalogue Facebook (Meta)")]
    assert order == sorted(order)


def test_each_element_id_exists_once():
    ids = re.findall(r'\sid="([^"$]+)"', HTML)
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates


def test_new_tab_in_menu_after_knowledge_base():
    nav = re.search(r"<nav>(.*?)</nav>", HTML, re.S).group(1)
    tabs = re.findall(r'data-tab="([a-z]+)"', nav)
    assert tabs.index("bob") == tabs.index("knowledge") + 1
    assert 'data-label="Réglages de Bob"' in nav
    assert '"bob", "settings"].forEach' in HTML  # l'onglet est bien masqué / affiché avec les autres


def test_each_tab_loads_what_it_shows():
    integrations = _show_tab_line("integrations")
    assert "loadWhatsAppStatus()" in integrations and "loadContactPoints()" in integrations
    bob = _show_tab_line("bob")
    for loader in ("loadNegotiationSettings()", "loadHandoffSettings()", "loadStrategySettings()", "loadFollowupSettings()"):
        assert loader in bob
    settings = _show_tab_line("settings")
    assert "loadMfaStatus()" in settings and "loadWhatsAppStatus" not in settings


def test_links_card_loads_products_when_opened_directly():
    """Le ciblage d'un produit (?p=SKU) a besoin de la liste des produits, même sans passer par Produits."""
    loader = re.search(r"async function loadContactPoints\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert 'if (!productsLoaded) { allProducts = await api("/api/v1/products")' in loader


def test_no_text_points_to_the_old_location():
    assert "Paramètres → Réponses aux objections" not in HTML
    assert "Réglages de Bob → Réponses aux objections" in HTML


# --- Lot 18 : système visuel, mode sombre -------------------------------------------------

def _block(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\n  \}", HTML, re.S)
    assert match, selector
    return match.group(1)


def test_dark_theme_redefines_every_color_token():
    light = set(re.findall(r"(--[a-z0-9-]+):", _block(":root")))
    dark = set(re.findall(r"(--[a-z0-9-]+):", _block('html[data-theme="dark"]')))
    layout_only = {"--radius", "--radius-sm", "--topbar-h", "--safe-top", "--safe-bottom"}
    assert light - layout_only - dark == set()


def test_theme_is_applied_before_first_paint_and_survives_blocked_storage():
    head = HTML.split("</head>")[0]
    assert 'document.documentElement.setAttribute("data-theme", theme)' in head
    assert 'try { theme = localStorage.getItem("bob_theme"); } catch (e) {}' in head
    assert "prefers-color-scheme: dark" in head


def test_theme_switch_in_sidebar():
    sidebar = re.search(r'<aside class="sidebar".*?</aside>', HTML, re.S).group(0)
    assert "setTheme('light')" in sidebar and "setTheme('dark')" in sidebar
    assert 'try { localStorage.setItem("bob_theme", theme); } catch (e) {}' in HTML


def test_menu_uses_line_icons_not_emojis():
    nav = re.search(r"<nav>(.*?)</nav>", HTML, re.S).group(1)
    assert nav.count("<svg") == nav.count("data-tab=")
    assert not re.search("[\U0001F300-\U0001FAFF☀-➿]", nav)


def test_logo_on_sidebar_and_login_and_favicon():
    assert HTML.count('class="brand-mark"') >= 6  # barre latérale + 5 écrans de connexion
    assert 'rel="icon" type="image/svg+xml"' in HTML


def test_no_hardcoded_colors_in_templates():
    """Toute couleur d'interface passe par les variables du thème (sinon illisible en mode sombre).
    Seule exception : l'image QR à partager, dessinée sur un canvas."""
    script = HTML.split("<script>", 2)[-1]
    offenders = [
        line.strip() for line in script.splitlines()
        if re.search(r"#[0-9A-Fa-f]{3,6}\b", line) and "ctx." not in line and "addColorStop" not in line
    ]
    assert offenders == []


def test_status_badges_show_french_labels():
    assert "STATUS_LABELS[c.status] || c.status" in HTML and "STATUS_LABELS[o.status] || o.status" in HTML
    assert 'WAITING_HUMAN: "Attend un humain"' in HTML


# --- Lot 19 : accueil -------------------------------------------------------------------

def test_home_is_first_and_existing_cards_stay_below():
    overview = _section("overview")
    order = [overview.index(x) for x in ('id="home-root"', "Détails", 'id="analytics-grid"', 'id="sales-card"',
                                         'id="signals-card"', 'id="strategies-card"')]
    assert order == sorted(order)
    assert "loadHome();" in re.search(r"async function loadOverview\(\) \{(.*?)\n\}", HTML, re.S).group(1)


def test_home_escapes_everything_that_comes_from_customers():
    render = re.search(r"function renderHome\(h\) \{(.*?)\n\}", HTML, re.S).group(1)
    for field in ("t.customer", "t.detail", "t.reason", "a.title", "a.detail", "h.first_name", "item.conversation_id"):
        assert f"${{esc({field}" in render or f"esc({field}" in HTML, field
    assert "${t.customer}" not in render and "${t.detail}" not in render and "${a.detail}" not in render


def test_home_actions_reuse_existing_navigation():
    assert "jumpToCustomerConversation('${esc(item.conversation_id)}')" in HTML
    assert "if (item.kind === \"ORDER\") return `showTab('orders')`" in HTML


# --- Lot 21 : catalogue en un clic ------------------------------------------------------------

def test_catalog_card_offers_one_click_and_keeps_manual_mode_folded():
    section = _section("integrations")
    assert 'id="meta-oauth-btn"' in section and "connectMetaCatalogOAuth()" in section
    assert '<details id="meta-advanced"' in section and 'id="meta-form"' in section
    assert "onclick=\"selectMetaCatalog('${esc(c.id)}')\">${esc(c.name)}</button>" in HTML
    assert "<li>${esc(e)}</li>" in HTML  # erreurs de synchronisation échappées


# --- Lot 23b : répondre au client, déconnecter le catalogue ----------------------------------

def test_reply_box_only_when_a_human_has_control_and_window_is_shown():
    detail = re.search(r'<div id="conv-detail">(.*?)\n      </div>\n    </section>', HTML, re.S).group(1)
    assert 'id="reply-box" class="hidden"' in detail and 'maxlength="4096"' in detail
    render = re.search(r"function renderReplyBox\(c\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert 'c.status === "WAITING_HUMAN"' in render and "c.reply_window_closes_at" in render
    assert "renderReplyBox(c);" in re.search(r"async function openConversation\(id\) \{(.*?)\n\}", HTML, re.S).group(1)


def test_failed_reply_keeps_the_text():
    send = re.search(r"async function sendHumanReply\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    try_part, catch_part = send.split("} catch (e) {")
    assert 'text.value = "";' in try_part and 'text.value = "";' not in catch_part
    assert '"/api/v1/messages/send"' in send and "is_proactive: false" in send


def test_catalog_can_be_disconnected_after_confirmation():
    section = _section("integrations")
    assert 'id="meta-disconnect-btn"' in section
    fn = re.search(r"async function disconnectMetaCatalog\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert fn.strip().startswith("if (!(await bobConfirm(") and 'method: "DELETE"' in fn  # lot 39 : fenêtre Bob
    assert 'getElementById("meta-disconnect-btn").classList.toggle("hidden", !metaConnected)' in HTML


# --- Lot 24 : type d'activité ----------------------------------------------------------------

def test_segment_screen_after_signup_escapes_options_and_can_be_left_by_logout():
    assert 'id="segment-screen" class="hidden" role="dialog"' in HTML
    check = re.search(r"async function checkBusinessTypeChosen\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert "if (bt.chosen) return;" in check
    assert "chooseBusinessType('${esc(o.code)}')" in check and "${sectorTitle(o)}" in check and "${esc(o.description)}" in check
    assert "return esc(option.label) + (option.hint ? ` <span class=\"option-hint\">(${esc(option.hint)})</span>` : \"\");" in HTML  # lot 53
    enter = re.search(r"function enterApp\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert "checkBusinessTypeChosen();" in enter
    logout = re.search(r"function logout\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert 'getElementById("segment-screen").classList.add("hidden")' in logout


def test_business_type_is_no_longer_changeable_from_bob_settings():
    """Lot 32 : choisi une fois à l'inscription, changé seulement par l'Admin."""
    section = _section("bob")
    assert 'id="business-type"' not in section and "saveBusinessType" not in HTML and "loadBusinessType" not in HTML
    assert 'id="business-type-label"' in section and "contactez le support Bob" in section
    assert "Ce choix est définitif" in HTML
    assert "${esc(o.code)}" in HTML and "${esc(o.label)}" in HTML  # écran de choix initial


def test_store_only_features_are_hidden_for_dealerships():
    assert "body.rdv-sector .store-only { display: none !important; }" in HTML  # lot 53 : concession et courtier
    nav = re.search(r"<nav>(.*?)</nav>", HTML, re.S).group(1)
    assert 'data-tab="orders" data-label="Commandes" class="store-only"' in nav
    assert 'class="card store-only" id="sales-card"' in HTML
    section = _section("bob")
    assert 'class="card store-only" style="max-width: 480px; flex: 1; min-width: 300px;">\n          <div class="label" style="margin-bottom: 4px; font-weight: 600;">Négociation de prix' in section
    assert '<th class="store-only">Upsell</th>' in HTML and 'data-label="Upsell" class="store-only"' in HTML
    apply = re.search(r"function applyBusinessType\(code, label\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert 'document.body.classList.toggle("dealership", dealership);' in apply


# --- Lot 25 : page Rendez-vous ------------------------------------------------------------------

def test_appointments_page_only_for_dealerships_and_loaded_with_its_tab():
    nav = re.search(r"<nav>(.*?)</nav>", HTML, re.S).group(1)
    assert 'data-tab="appointments" data-label="Rendez-vous" id="nav-appointments" class="hidden"' in nav
    apply = re.search(r"function applyBusinessType\(code, label\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert 'const dealership = code === "CAR_DEALERSHIP";' in apply
    assert 'getElementById("nav-appointments").classList.toggle("hidden", !(dealership || insurance))' in apply  # lot 53
    assert HTML.count("applyBusinessType(") >= 2  # définition + chargement
    assert "loadAppointments(currentAppointmentView)" in _show_tab_line("appointments")
    assert "if (item.kind === \"APPOINTMENT\") return `showTab('appointments')`" in HTML


def test_appointment_cards_escape_everything_from_customers_and_bob():
    card = re.search(r"function appointmentCard\(a, view\) \{(.*?)\n\}", HTML, re.S).group(1)
    for field in ("a.need", "a.budget", "a.trade_in", "a.notes", "a.kind_label", "a.vehicle", "a.customer",
                  "a.availability", "a.scheduled_label", "a.id", "a.conversation_id"):
        assert f"esc({field})" in card, field
    for raw in ("${a.need}", "${a.notes}", "${a.customer}", "${a.availability}", "${a.vehicle}"):
        assert raw not in card
    assert '${notifyVia ? "checked" : "disabled"}' in card  # prévenir le client : coché par défaut


# --- Lot 26 : fiches véhicules, et produits enfin échappés -------------------------------------

def test_product_data_is_escaped_everywhere_it_is_displayed():
    """Les produits peuvent venir de Meta, Shopify ou d'un CSV : jamais interprétés comme du HTML."""
    rows = re.search(r"async function loadProducts\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    for field in ("p.name", "p.sku", "p.image_url", "p.currency"):
        assert f"esc({field})" in rows, field
    assert "${p.name}" not in rows and "${p.sku}" not in rows and '${p.image_url}"' not in rows
    edit = re.search(r"function renderEditPanel\(productId\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert 'value="${esc(p.name)}"' in edit and '${esc(p.description || "")}' in edit
    assert "${p.name} (${p.sku})" not in HTML
    assert "${c.product_name}" not in HTML and "${e.title}" not in HTML and "${e.content}" not in HTML


def test_vehicle_sheet_fields_only_for_dealerships_and_escaped():
    apply = re.search(r"function applyBusinessType\(code, label\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert 'getElementById("new-vehicle-fields")' in apply and 'getElementById("csv-vehicle-help")' in apply
    fields = re.search(r"function vehicleFields\(prefix, v\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert 'value="${esc(v[key] ?? "")}"' in fields
    assert 'if (currentBusinessType === "CAR_DEALERSHIP") payload.vehicle = readVehicle("new");' in HTML
    rows = re.search(r"async function loadProducts\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert "esc(vehicleSummary(p.vehicle))" in rows


# --- Lot 36 : incident — une constante déclarée deux fois bloquait tout le tableau de bord -------

@pytest.mark.parametrize("page", ["app/static/dashboard/index.html", "app/static/instant-demo/index.html",
                                  "app/static/superadmin/index.html"])
def test_page_scripts_are_valid_javascript(page, tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent : vérification de syntaxe JavaScript impossible ici")
    html = open(page, encoding="utf-8").read()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts
    for i, script in enumerate(scripts):
        path = tmp_path / f"script{i}.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
