import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import JSON, Date, DateTime, Enum, Numeric, String, Text, Uuid, func
from sqlalchemy import true as sa_true
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TenantPlan(StrEnum):
    """
    Grille de plans définitive (fige toute segmentation précédente — company_size,
    plan_id, is_paid). FREE = freemium ; les 4 autres = payant.
    """

    FREE = "FREE"
    INDEPENDANT = "INDEPENDANT"
    STARTER = "STARTER"
    PRO = "PRO"
    ENTERPRISE = "ENTERPRISE"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)  # ISO 3166-1 alpha-2
    currency: Mapped[str] = mapped_column(String(3), nullable=False)  # ISO 4217
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    website_url: Mapped[str | None] = mapped_column(String(500))
    # Lot 78 — catégorie Bob proposée par défaut pour les nouveaux produits (« je vends surtout de la mode »).
    default_bob_category: Mapped[str | None] = mapped_column(String(24))
    company_profile: Mapped[str | None] = mapped_column(Text)  # présentation libre — injectée dans le prompt de l'agent

    # Section 13 — commission sur ventes. Taux fixé UNIQUEMENT par le Super Admin (négocié
    # B2B, jamais modifiable par le commerçant lui-même). NULL = pas de commission due.
    commission_rate: Mapped[float | None] = mapped_column(Numeric(5, 2))  # ex. 5.00 = 5%
    payment_link: Mapped[str | None] = mapped_column(String(500))  # lien Wave/Orange Money/etc. du commerce
    is_demo: Mapped[bool] = mapped_column(default=False, nullable=False)

    plan: Mapped[TenantPlan] = mapped_column(Enum(TenantPlan, name="tenant_plan"), default=TenantPlan.FREE, nullable=False)
    plan_assigned_by: Mapped[str] = mapped_column(String(64), default="AUTO")

    active: Mapped[bool] = mapped_column(default=True)
    # Lot 51 — abonnement : dernier jour payé, saisi par le Super Admin (vide = pas d'échéance, Bob ne se
    # met jamais en pause pour impayé). Règles dans app/services/bob_pause.py. Le dernier email d'échéance
    # envoyé (WARNING / GRACE / PAUSED) est noté avec la date qu'il concernait : une nouvelle date repart à zéro.
    paid_until: Mapped[date | None] = mapped_column(Date)
    billing_notice_stage: Mapped[str | None] = mapped_column(String(16))
    billing_notice_for: Mapped[date | None] = mapped_column(Date)
    # Lot 60 — rapport mensuel aux administrateurs (7 jours avant le renouvellement, ou début de mois) :
    # quand le dernier est parti et jusqu'où il comptait (le suivant reprend à cette date).
    report_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Lot 24 — type d'activité (app/services/business_type.py). chosen_at vide = le commerçant
    # n'a pas encore choisi (l'écran « Votre secteur » s'affiche après l'inscription).
    business_type: Mapped[str] = mapped_column(String(32), default="ONLINE_STORE", server_default="ONLINE_STORE", nullable=False)
    business_type_chosen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lot 38 — « VOUS » ou « TU » envers les clients (boutique en ligne ; concession : toujours VOUS).
    address_form: Mapped[str] = mapped_column(String(8), default="VOUS", server_default="VOUS", nullable=False)
    # Lot 54 — courtier / agent d'assurance (règlement CIMA 01-24) : qui parle au client. Statut (COURTIER,
    # AGENCE_GENERALE, AGENT ; vide = pas encore indiqué), compagnie mandante, numéro d'agrément et contact
    # pour les réclamations. Bob les donne au client quand il les demande ; il n'invente jamais un agrément.
    insurance_structure: Mapped[str | None] = mapped_column(String(24))
    insurer_name: Mapped[str | None] = mapped_column(String(150))
    insurance_license: Mapped[str | None] = mapped_column(String(80))
    complaints_contact: Mapped[str | None] = mapped_column(String(200))
    # Lot 55 — échéances du registre des contrats : rappel automatique au client (réglable, actif par défaut),
    # et jour du dernier récapitulatif des échéances envoyé au cabinet (un seul email par jour).
    renewal_reminders_enabled: Mapped[bool] = mapped_column(default=True, server_default=sa_true(), nullable=False)
    renewal_digest_on: Mapped[date | None] = mapped_column(Date)
    # Lot 63 — commerce : Bob relance les commandes non payées (actif par défaut, désactivable).
    order_reminders_enabled: Mapped[bool] = mapped_column(default=True, server_default=sa_true(), nullable=False)
    # Lot 68 — modèles WhatsApp autorisés pour cette boutique (le Super Admin peut les couper, ex. compte d'examen Meta).
    wa_templates_allowed: Mapped[bool] = mapped_column(default=True, server_default=sa_true(), nullable=False)
    # Lot 69 — alerte de plafond de conversations déjà envoyée ce mois-là (« 2026-10 ») et à quel seuil (80 puis 100).
    quota_alert_month: Mapped[str | None] = mapped_column(String(7))
    quota_alert_level: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    # Lot 57 — délai de réponse aux réclamations annoncé au client, en jours ouvrés (CIMA 01-24, art. 11).
    complaint_delay_days: Mapped[int] = mapped_column(default=10, server_default="10", nullable=False)
    # Lot 58 (CIMA 01-24) — art. 12 : raison sociale et adresse de la compagnie (agent, agence générale) ou des
    # compagnies partenaires (courtier : [{"name", "address"}]) ; art. 11 : lien vers les conditions tarifaires
    # publiques ; art. 7 : lien vers la politique de confidentialité. Bob les donne, ne les invente jamais.
    insurer_legal_name: Mapped[str | None] = mapped_column(String(150))
    insurer_address: Mapped[str | None] = mapped_column(String(300))
    insurance_partners: Mapped[list | None] = mapped_column(JSON)
    tariff_url: Mapped[str | None] = mapped_column(String(300))
    privacy_policy_url: Mapped[str | None] = mapped_column(String(300))
    # Lot 79 — agence immobilière (loi Hoguet, loi ALUR) : statut, carte professionnelle ou RSAC, réseau, garantie
    # financière, activités (vente, location, gestion), secteur couvert, lien vers le barème d'honoraires, médiateur.
    # Clés et valeurs vérifiées par app/services/real_estate.py ; Bob les donne, ne les invente jamais.
    real_estate_profile: Mapped[dict | None] = mapped_column(JSON)
    # Lot 80 — alertes « nouveau bien » (plan Pro) : envoi automatique, et modèles WhatsApp payants hors 20 h.
    property_alerts_auto: Mapped[bool] = mapped_column(default=False, server_default=sa_false(), nullable=False)
    property_alert_templates: Mapped[bool] = mapped_column(default=False, server_default=sa_false(), nullable=False)
    # Lot 83 — demandes reçues par email (portails, formulaire du site) : code de l'adresse de l'agence
    # (bob.contacts+CODE@…), envoi automatique (sinon l'agent valide chaque réponse), dernier code de confirmation
    # du transfert Gmail reçu pour l'agence.
    lead_inbox_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    lead_auto_send: Mapped[bool] = mapped_column(default=False, server_default=sa_false(), nullable=False)
    lead_gmail_code: Mapped[str | None] = mapped_column(String(32))
    lead_gmail_code_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lot 41 — carte « Premiers pas » de l'accueil masquée par le commerçant.
    onboarding_hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lot 65 — pré-inscription (avant le lancement) : compte créé pendant le « mode pré-inscription ». Tant que
    # l'accès n'est pas ouvert (access_opened_at), seul l'accueil d'exemple est visible et rien ne fonctionne.
    # Offre de lancement figée à l'inscription ; informations préparées par le client pour l'installation.
    prereg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    launch_offer: Mapped[str | None] = mapped_column(String(32))
    prep_whatsapp: Mapped[str | None] = mapped_column(String(40))
    prep_catalog_size: Mapped[str | None] = mapped_column(String(16))
    prep_note: Mapped[str | None] = mapped_column(String(500))
    prep_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def is_paid(self) -> bool:
        """Calculé depuis `plan`, jamais stocké séparément — plus aucune double source de vérité."""
        return self.plan != TenantPlan.FREE
