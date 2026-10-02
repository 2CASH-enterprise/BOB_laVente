"""Lot 39 — infobulles et fenêtres aux couleurs de Bob (plus aucune fenêtre grise du navigateur)."""
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
SCRIPT = HTML[HTML.index("const API_BASE"):]


def _function(name: str) -> str:
    match = re.search(r"(?:async )?function " + name + r"\([^)]*\) \{(.*?)\n\}", SCRIPT, re.S)
    assert match, name
    return match.group(1)


def _bubbles() -> list[str]:
    return re.findall(r'<span class="tip-bubble" hidden>(.*?)</span>(?=</div>|<)', HTML, re.S)


def test_no_browser_dialog_is_left():
    calls = re.findall(r"(?<![\w.])(alert|confirm|prompt)\s*\(", SCRIPT)
    assert calls == [], f"fenêtres du navigateur restantes : {calls}"


def test_dialog_and_toast_insert_text_never_html():
    for name in ("bobDialog", "toast"):
        body = _function(name)
        assert "textContent" in body
        assert "innerHTML" not in body, name  # un nom de client ou de produit ne peut rien injecter


def test_dialog_is_accessible():
    body = _function("bobDialog")
    assert 'setAttribute("aria-modal", "true")' in body and "aria-labelledby" in body
    assert 'e.key === "Escape"' in body and 'e.key === "Tab"' in body  # Échap ferme, le focus reste dedans
    assert "previousFocus.focus()" in body


def test_errors_stay_until_closed_and_successes_disappear():
    body = _function("toast")
    assert 'if (kind !== "error") setTimeout(() => item.remove(), 4000)' in body
    assert 'setAttribute("role", "alert")' in body


def test_destructive_actions_are_red():
    for name in ("deleteProduct", "deleteQrCode", "archiveContactPoint", "cancelOrder", "deleteKnowledgeEntry",
                 "disconnectMetaCatalog", "revokeAllSessions", "cancelAppointment", "toggleContactPoint"):
        assert "danger: true" in _function(name), name


def test_order_cancellation_is_a_single_window():
    body = _function("cancelOrder")
    assert body.count("bobDialog(") == 1 and "bobConfirm(" not in body
    assert 'name: "reason"' in body and 'name: "notify", type: "checkbox", value: false' in body
    assert "reason: reason.trim() || null, notify_customer: notify" in body  # même envoi au serveur qu'avant


def test_appointment_cancellation_is_a_single_window():
    body = _function("cancelAppointment")
    assert body.count("bobDialog(") == 1
    assert "const notify = canNotify && answer.notify;" in body


def test_link_owner_is_one_form_with_email_check():
    body = _function("editContactPointOwner")
    assert body.count("bobDialog(") == 1 and 'name: "email", type: "email"' in body
    assert "Adresse email invalide." in body
    assert "VOTRE_EMAIL_ICI" in body  # jamais une vraie adresse en exemple


def test_explanations_moved_into_infobulles():
    bubbles = " ".join(_bubbles())
    for text in ("Bob cède progressivement", "Colonnes attendues", "Le QR ne change jamais", "Un lien à coller partout",
                 "Un lien par commercial", "Ce qui décide si Bob vous passe la main", "Toujours actif : une question sur les retours",
                 "Quand un client exprime un frein", "Bob s'appuie sur ces informations", "S'applique aux réponses de Bob",
                 "Les pubs Facebook et Instagram", "Un code à 6 chiffres vous sera envoyé", "Téléphone perdu",
                 "Ajoutez l'icône Bob", "Une pastille rouge", "Quand un client ne répond plus, Bob lui envoie un email"):
        assert text in bubbles, text
        assert HTML.count(text) == 1, f"texte en double (resté visible ?) : {text}"


def test_warnings_stay_visible():
    bubbles = " ".join(_bubbles())
    for text in ("Envoyé uniquement aux clients ayant explicitement accepté", "accepté de recevoir vos offres reçoivent ces emails"):
        assert text in HTML and text not in bubbles, text


def test_page_explanations_open_from_the_page_title():
    templates = re.findall(r'<section id="tab-(\w+)" class="hidden">\s*<template class="page-tip-src">', HTML)
    assert set(templates) == {"appointments", "bob"}
    assert 'id="appt-timezone"' in HTML  # le fuseau horaire reste affiché
    assert '<strong id="business-type-label"></strong>' in HTML  # l'activité reste affichée
    body = _function("showTab")
    assert "template.page-tip-src" in body and ".page-tip-btn" in body
    assert HTML.count("page-tip-btn\"></button>") == 2  # titre (ordinateur) et barre du haut (téléphone)


def test_every_infobulle_button_has_its_bubble():
    markup = HTML[:HTML.index("<script>", HTML.index("<body"))]
    assert len(re.findall(r'<button class="tip(?: hidden page-tip-btn)?"></button><span class="tip-bubble', markup)) \
        == len(re.findall(r'<button class="tip', markup)) == 16  # lot 49 : relances par email
    assert "initTips();" in SCRIPT
