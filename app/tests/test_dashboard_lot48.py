"""Lot 48 — accueil au vouvoiement, et choix de fichier aux couleurs de Bob (import CSV, photo produit)."""
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


def test_home_says_vous():
    body = HTML[HTML.index("function renderHome(h) {"):HTML.index("async function loadHome() {")]
    for tu in ("t'attend", "pour toi", "tes clients"):
        assert tu not in body, tu
    for vous in ('"s vous attendent" : " vous attend"', "Aucun client ne vous attend", "s'occupe de vos clients",
                 "Voici ce que Bob a fait pour vous"):
        assert vous in body, vous


def test_no_tutoiement_left_in_the_dashboard():
    visible = re.sub(r"//[^\n]*|/\*.*?\*/|<!--.*?-->", "", HTML, flags=re.S)
    assert not re.search(r"\bt'attend|\bpour toi\b|\btes clients\b|\bton tableau\b", visible)


def test_every_file_field_has_the_bob_design():
    inputs = re.findall(r'<input type="file"[^>]*>', HTML)
    assert len(inputs) == 3  # lot 55 : import du registre des contrats
    for match in re.finditer(r'<input type="file"', HTML):
        before = HTML[max(0, match.start() - 1500):match.start()]
        assert before.rfind('class="file-drop"') > before.rfind("</label>") or before.rfind('class="file-pick"') > before.rfind("</label>")
    assert all("aria-label=" in i for i in inputs)


def test_csv_picker_shows_the_file_and_accepts_drops():
    zone = HTML[HTML.index('<label class="file-drop" id="csv-drop">'):]
    zone = zone[:zone.index("</label>")]
    assert 'id="csv-file-input"' in zone and 'onchange="showChosenCsv()"' in zone and "Choisir un fichier CSV" in zone
    js = HTML[HTML.index("function showChosenCsv() {"):HTML.index("async function importCsv() {")]
    assert "file.name" in js and "has-file" in js and "Ko" in js
    assert 'addEventListener("drop"' in js and "\\.csv$" in js and "Seuls les fichiers .csv" in js and "new DataTransfer()" in js
    importer = HTML[HTML.index("async function importCsv() {"):HTML.index("async function loadCompanyProfile()")]
    assert 'fileInput.value = "";' in importer and "showChosenCsv();" in importer
    assert "initCsvDrop();" in HTML[HTML.index("async function loadProducts() {"):][:200]


def test_file_styles_cover_both_themes_with_tokens():
    css = HTML[HTML.index(".file-drop {"):HTML.index(".sr-only {")]
    assert "var(--accent)" in css and "var(--border-strong)" in css and "opacity: 0" in css
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", css)  # seulement des jetons de couleur : clair et sombre
