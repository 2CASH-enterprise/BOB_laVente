from app.services.csv_column_mapper import map_csv_to_canonical_format


def test_recognizes_common_synonyms():
    raw = "Article,Prix vente,Qté,Description\niPhone 15,450000,5,Neuf"
    mapped = map_csv_to_canonical_format(raw, default_currency="XOF")
    assert "NAME" in mapped.splitlines()[0]
    assert "iPhone 15" in mapped
    assert "450000" in mapped


def test_generates_sku_when_missing():
    raw = "Nom,Prix\nProduit A,1000\nProduit B,2000"
    mapped = map_csv_to_canonical_format(raw, default_currency="XOF")
    assert "DEMO-0001" in mapped
    assert "DEMO-0002" in mapped


def test_fills_default_currency_when_missing():
    raw = "Nom,Prix\nProduit A,1000"
    mapped = map_csv_to_canonical_format(raw, default_currency="EUR")
    assert ",EUR," in mapped


def test_preserves_explicit_currency_column():
    raw = "Nom,Prix,Devise\nProduit A,1000,USD"
    mapped = map_csv_to_canonical_format(raw, default_currency="XOF")
    assert ",USD," in mapped
    assert ",XOF," not in mapped


def test_case_and_accent_insensitive_matching():
    raw = "PRODUIT,PRIX DE VENTE\nChaise,5000"
    mapped = map_csv_to_canonical_format(raw, default_currency="XOF")
    assert "Chaise" in mapped
    assert "5000" in mapped


def test_defaults_stock_to_zero_when_missing():
    raw = "Nom,Prix\nProduit A,1000"
    mapped = map_csv_to_canonical_format(raw, default_currency="XOF")
    lines = mapped.splitlines()
    assert lines[1].split(",")[6] == "0"  # colonne STOCK


def test_empty_csv_raises_value_error():
    import pytest

    with pytest.raises(ValueError):
        map_csv_to_canonical_format("", default_currency="XOF")
