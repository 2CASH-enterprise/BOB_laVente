"""
Lot 78 — fiches produits par catégorie (secteur Commerce, France d'abord ; décisions du 16/10).

- 7 catégories Bob FIXES (distinctes des catégories libres du commerçant). Une boutique peut en mélanger plusieurs.
- Obligatoires (inchangés) : nom, prix, stock — plus la catégorie Bob, proposée par mots-clés si absente.
- Tout le reste est FACULTATIF : des champs communs, puis 2 à 4 champs CLÉS par catégorie (comptés dans le taux
  de complétude) et des champs facultatifs.
- Chaque catégorie donne à Bob ses questions de qualification (le levier de conversion).
- Bob ne dit que ce qui est écrit : un champ vide = « je n'ai pas l'information », jamais une supposition.
- Les variantes (taille, couleur…) avec LEUR stock viennent de Shopify (`products.variants`) ; une boutique hors
  ligne les écrit dans le champ texte « Tailles / couleurs disponibles ».
- Aucune IA ici : la catégorie est proposée par mots-clés (décision du 16/10 : pas de coût supplémentaire).
"""
import re
import unicodedata

GENERAL = "GENERAL"
MAX_VALUE = 400       # caractères par champ
MAX_VARIANTS_FOR_AI = 40

# Champs communs, tous facultatifs. (clé, libellé, aide)
COMMON_FIELDS = [
    ("brand", "Marque", "Ex. Nike, Samsung, Shea Moisture"),
    ("variants_text", "Tailles / couleurs disponibles", "Ex. 38 à 44 ; noir, bleu (hors Shopify)"),
    ("compare_at_price", "Prix barré", "Prix avant remise, si le produit est en promotion"),
    ("highlights", "Points forts", "3 au plus, une ligne chacun : les arguments de vente"),
    ("delivery", "Livraison (ce produit)", "Seulement si différente de la règle de la boutique"),
    ("warranty", "Garantie (ce produit)", "Ex. 2 ans constructeur"),
    ("ean", "Code-barres (EAN)", ""),
]

CATEGORIES: dict[str, dict] = {
    "FASHION": {
        "label": "Mode, chaussures et accessoires",
        "key": [("sizes", "Tailles ou pointures", "Ex. S à XL, 38 à 44"), ("colors", "Couleurs", "Ex. noir, camel"),
                ("material", "Matière", "Ex. 100 % coton, cuir")],
        "optional": [("gender", "Pour", "Femme, homme, enfant, mixte"), ("fit", "Coupe", "Ajustée, droite, oversize"),
                     ("size_guide", "Guide des tailles", "Ex. taille normalement, prendre une taille au-dessus"),
                     ("care", "Entretien", "Ex. lavage 30 °C"), ("occasion", "Occasion", "Quotidien, soirée, cérémonie"),
                     ("dimensions", "Dimensions (sacs)", "L × l × H"), ("origin", "Origine", "Pays de fabrication")],
        "questions": ["Quelle taille ou pointure portez-vous ?", "C'est pour quelle occasion ?", "Plutôt quelle couleur ?"],
    },
    "BEAUTY": {
        "label": "Beauté, soins et cheveux",
        "key": [("product_kind", "Type de produit", "Sérum, crème, shampoing, perruque…"),
                ("usage_zone", "Usage / zone", "Visage, corps, cheveux"), ("volume", "Contenance", "Ex. 250 ml, 100 g")],
        "optional": [("skin_hair_type", "Type de peau ou de cheveux", "Sèche, grasse, sensible, crépus…"),
                     ("ingredients", "Ingrédients (INCI)", ""), ("how_to", "Mode d'emploi", ""),
                     ("precautions", "Précautions", ""), ("shade", "Teinte", ""), ("fragrance", "Parfum", "Parfumé ou non"),
                     ("hair_length", "Longueur (cheveux)", "Ex. 18 pouces"), ("hair_texture", "Texture (cheveux)", "Lisse, ondulé, bouclé"),
                     ("hair_material", "Cheveux naturels ou synthétiques", "")],
        "questions": ["Plutôt peau ou cheveux secs, gras, sensibles ?", "Quel résultat cherchez-vous ?",
                      "Pour les cheveux : quelle longueur et quelle texture ?"],
    },
    "ELECTRONICS": {
        "label": "Téléphonie, électronique et accessoires",
        "key": [("brand_model", "Marque et modèle", "Ex. Samsung Galaxy A15"),
                ("compatibility", "Compatibilité", "Appareils et connectique, ex. USB-C, iPhone 15"),
                ("condition", "État", "Neuf, reconditionné, occasion")],
        "optional": [("storage", "Stockage / mémoire", "Ex. 128 Go, 6 Go de RAM"), ("battery", "Batterie", "Ex. 5000 mAh"),
                     ("charging_power", "Puissance de charge", "Ex. 25 W"), ("box_contents", "Contenu de la boîte", ""),
                     ("refurb_grade", "Grade de reconditionnement", "Ex. très bon état")],
        "questions": ["Quel téléphone ou quel appareil avez-vous ?", "C'est pour quel usage ?", "Plutôt neuf ou reconditionné ?"],
    },
    "APPLIANCES": {
        "label": "Électroménager",
        "key": [("brand_model", "Marque et modèle", ""), ("dimensions", "Dimensions (H × L × P)", "Ex. 180 × 60 × 65 cm"),
                ("capacity", "Capacité", "Litres, kg, couverts")],
        "optional": [("energy_class", "Classe énergétique", "Ex. D"), ("power", "Puissance", "Ex. 1800 W"),
                     ("consumption", "Consommation", "Ex. 250 kWh/an"), ("noise", "Niveau sonore", "Ex. 39 dB"),
                     ("installation", "Installation / livraison à l'étage", ""), ("repairability", "Indice de réparabilité", "Ex. 7,2/10")],
        "questions": ["Quelle place avez-vous (dimensions) ?", "Pour combien de personnes ?", "Faut-il l'installation ?"],
    },
    "HOME": {
        "label": "Maison, meubles et décoration",
        "key": [("dimensions", "Dimensions (L × l × H)", "Ex. 200 × 90 × 80 cm"), ("material", "Matière", "Bois, métal, verre…"),
                ("finish", "Couleur / finition", "")],
        "optional": [("assembly", "Montage", "Livré monté ou à monter"), ("weight", "Poids", ""), ("max_load", "Charge maximale", ""),
                     ("care", "Entretien", ""), ("indoor_outdoor", "Intérieur / extérieur", ""), ("package_contents", "Contenu du colis", "")],
        "questions": ["C'est pour quelle pièce ?", "Quelle place avez-vous ?", "Plutôt quel style ?"],
    },
    "FOOD": {
        "label": "Alimentation et boissons",
        "key": [("quantity", "Contenance / poids", "Ex. 1 kg, 75 cl"), ("origin", "Origine", "Ex. Côte d'Ivoire"),
                ("shelf_life", "Date limite / durée de conservation", "")],
        "optional": [("allergens", "Allergènes", "Ex. contient : arachide, gluten"), ("ingredients", "Ingrédients", ""),
                     ("storage", "Conservation", "Frais, sec, surgelé"), ("labels", "Labels", "Halal, bio, végan — seulement si certifié"),
                     ("alcohol", "Degré d'alcool", "Ex. 12 % vol.")],
        "questions": ["C'est pour combien de personnes ?", "Une allergie ou un régime à respecter ?"],
    },
    GENERAL: {
        "label": "Général / autres produits",
        "key": [],
        "optional": [("features", "Caractéristiques", "Une par ligne, « nom : valeur » (6 au plus), ex. Compatible : Yamaha NMAX")],
        "questions": ["Qu'est-ce que vous cherchez exactement ?", "C'est pour quel usage ?"],
    },
}

# Lot 78 — catégorie proposée par mots-clés (nom, type de produit Shopify, étiquettes). Le commerçant corrige.
KEYWORDS = {
    "FASHION": ["robe", "jupe", "pantalon", "jean", "chemise", "t-shirt", "tee-shirt", "tshirt", "pull", "sweat", "veste",
                "manteau", "blouson", "chaussure", "basket", "sneaker", "sandale", "escarpin", "botte", "sac", "pochette",
                "ceinture", "bijou", "collier", "bracelet", "boucle d'oreille", "bague", "montre", "casquette", "chapeau",
                "foulard", "echarpe", "wax", "boubou", "pagne", "kente", "dress", "shirt", "shoe", "shoes", "bag",
                "jewelry", "jewellery", "apparel", "clothing", "vetement", "lingerie", "maillot", "short", "legging"],
    "BEAUTY": ["creme", "serum", "shampoing", "shampooing", "apres-shampoing", "savon", "maquillage", "rouge a levres",
               "mascara", "fond de teint", "parfum", "perruque", "meche", "tissage", "lace", "wig", "frontal", "closure",
               "cheveux", "karite", "lotion", "soin", "gommage", "deodorant", "vernis", "beauty", "skincare", "hair",
               "cosmetique", "huile capillaire", "masque capillaire"],
    "ELECTRONICS": ["telephone", "smartphone", "iphone", "samsung", "tecno", "infinix", "xiaomi", "chargeur", "cable",
                    "ecouteur", "ecouteurs", "airpods", "casque audio", "montre connectee", "tablette", "ipad", "ordinateur",
                    "laptop", "pc portable", "enceinte", "powerbank", "batterie externe", "coque", "protection ecran",
                    "phone", "charger", "headphones", "electronics", "console", "manette"],
    "APPLIANCES": ["refrigerateur", "frigo", "congelateur", "lave-linge", "machine a laver", "lave-vaisselle", "micro-ondes",
                   "four", "climatiseur", "ventilateur", "mixeur", "blender", "cuisiniere", "plaque de cuisson", "aspirateur",
                   "fer a repasser", "bouilloire", "cafetiere", "friteuse", "robot", "appliance"],
    "HOME": ["canape", "table", "chaise", "lit", "matelas", "armoire", "etagere", "lampe", "luminaire", "rideau", "tapis",
             "coussin", "decoration", "deco", "vase", "cadre", "meuble", "drap", "couette", "vaisselle", "assiette",
             "bougie", "miroir", "furniture", "home"],
    "FOOD": ["riz", "farine", "epice", "epices", "sauce", "boisson", "jus", "bissap", "gingembre", "cafe", "the", "chocolat",
             "biscuit", "attieke", "manioc", "igname", "plantain", "poisson", "viande", "poulet", "vin", "biere", "eau",
             "conserve", "pates", "huile de palme", "huile alimentaire", "fonio", "gari", "food", "drink", "epicerie"],
}


def _plain(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text.lower())


def is_category(code: str | None) -> bool:
    return code in CATEGORIES


def label(code: str | None) -> str:
    return CATEGORIES.get(code or GENERAL, CATEGORIES[GENERAL])["label"]


def guess_category(*texts: str | None) -> str:
    """Catégorie la plus probable d'après les mots ; GENERAL si rien ne ressort."""
    plain = " " + " ".join(_plain(t) for t in texts if t) + " "
    best, best_score = GENERAL, 0
    for code, words in KEYWORDS.items():
        score = sum(1 for w in words if re.search(rf"(?<![a-z]){re.escape(w)}s?(?![a-z])", plain))
        if score > best_score:
            best, best_score = code, score
    return best


def fields_of(code: str | None) -> list[tuple[str, str, str]]:
    cat = CATEGORIES.get(code or GENERAL, CATEGORIES[GENERAL])
    return COMMON_FIELDS + cat["key"] + cat["optional"]


def normalize_sheet(code: str | None, raw: dict | None) -> dict | None:
    """Garde seulement les champs connus de la catégorie (et communs), textes nettoyés et bornés."""
    if not raw:
        return None
    allowed = {key for key, _, _ in fields_of(code)}
    sheet = {}
    for key, value in raw.items():
        if key not in allowed or value is None:
            continue
        if key == "compare_at_price":
            try:
                number = round(float(str(value).replace(",", ".").replace(" ", "")), 2)
            except ValueError:
                continue
            if number > 0:
                sheet[key] = number
            continue
        text = str(value).strip()[:MAX_VALUE]
        if text:
            sheet[key] = text
    return sheet or None


def completeness(product) -> int | None:
    """Taux de remplissage de la fiche (0-100) : photo, description et champs clés de la catégorie."""
    code = getattr(product, "bob_category", None)
    if not code:
        return None
    sheet = getattr(product, "sheet", None) or {}
    keys = [key for key, _, _ in CATEGORIES.get(code, CATEGORIES[GENERAL])["key"]] or ["features"]
    checks = [bool(getattr(product, "image_url", None)), bool((getattr(product, "description", None) or "").strip())]
    for key in keys:
        filled = bool(sheet.get(key))
        if key in ("sizes", "colors") and getattr(product, "variants", None):
            filled = True  # Shopify : les variantes disent déjà les tailles et couleurs
        checks.append(filled)
    return round(100 * sum(checks) / len(checks))


def _variant_label(variant: dict) -> str:
    options = variant.get("options") or {}
    return " / ".join(str(v) for v in options.values() if v) or (variant.get("title") or "Unique")


def variants_for_ai(variants: list | None) -> dict | None:
    """Variantes disponibles et épuisées, telles que Shopify les donne (jamais devinées)."""
    if not variants:
        return None
    if len(variants) == 1 and not (variants[0].get("options") or {}):
        return None
    available = [_variant_label(v) for v in variants if (v.get("stock") or 0) > 0]
    sold_out = [_variant_label(v) for v in variants if (v.get("stock") or 0) <= 0]
    out = {"disponibles": available[:MAX_VARIANTS_FOR_AI]}
    if sold_out:
        out["épuisées"] = sold_out[:MAX_VARIANTS_FOR_AI]
    return out


def sheet_for_ai(product) -> dict | None:
    """Ce que Bob reçoit : la catégorie, les champs remplis avec leur libellé, les variantes."""
    code = getattr(product, "bob_category", None)
    sheet = getattr(product, "sheet", None) or {}
    variants = variants_for_ai(getattr(product, "variants", None))
    if not code and not sheet and not variants:
        return None
    out: dict = {"catégorie": label(code)}
    for key, name, _ in fields_of(code):
        if sheet.get(key) not in (None, ""):
            out[name] = sheet[key]
    if variants:
        out["variantes"] = variants
    return out


def definitions() -> list[dict]:
    """Pour le formulaire du tableau de bord."""
    def pack(items):
        return [{"key": k, "label": lab, "hint": hint} for k, lab, hint in items]

    return [{"code": code, "label": cat["label"], "key_fields": pack(cat["key"]), "optional_fields": pack(cat["optional"]),
             "questions": cat["questions"]} for code, cat in CATEGORIES.items()]


def common_definitions() -> list[dict]:
    return [{"key": k, "label": lab, "hint": hint} for k, lab, hint in COMMON_FIELDS]


def questions_block(codes: set[str]) -> str:
    """Questions de qualification des catégories présentes dans le catalogue (pour les consignes de Bob)."""
    lines = []
    for code, cat in CATEGORIES.items():
        if code in codes:
            lines.append(f"- {cat['label']} : " + " / ".join(cat["questions"]))
    if not lines:
        return ""
    return ("\n\nQUESTIONS UTILES PAR CATÉGORIE (une seule à la fois, seulement si le client n'a pas déjà répondu)\n\n"
            + "\n".join(lines))
