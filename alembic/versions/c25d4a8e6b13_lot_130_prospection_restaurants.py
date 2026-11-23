"""Lot 130 — prospection des restaurants par Naomy : interrupteur (éteint) et remise à zéro des prospects restaurants.

Remise à zéro (une seule fois) : les restaurants « en cours », sans compte, repassent à « ajouté, jamais contacté » —
contacts, clics, démo, campagne et étape d'e-mail effacés, e-mails de prospection déjà envoyés retirés de la liste des
envois. L'historique de la fiche est gardé : les anciens contacts, clics et démos deviennent des notes « avant la remise
à zéro », et une note « remis à zéro » est ajoutée. Jamais touchés : désinscrits, invalides, « pas intéressé », comptes
créés, et toute la liste d'opposition (prospect_suppressions).

Revision ID: c25d4a8e6b13
Revises: c24b7e3a9d51
"""
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "c25d4a8e6b13"
down_revision = "c24b7e3a9d51"
branch_labels = None
depends_on = None

NOTE = "Remis à zéro (lot 130) : la prospection des restaurants repart par Naomy"


def upgrade() -> None:
    op.add_column("prospection_settings", sa.Column("restaurants_enabled", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("prospection_settings", sa.Column("restaurants_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("prospection_settings", sa.Column("restaurants_changed_by", sa.String(length=255), nullable=True))

    bind = op.get_bind()
    ids = [row[0] for row in bind.execute(sa.text(
        "select id from prospects where sector = 'RESTAURANT' and status = 'ACTIVE' and tenant_id is null"))]
    if not ids:
        return
    prospects = sa.table("prospects", sa.column("id"), sa.column("contacted_at"), sa.column("last_contact_at"),
                         sa.column("first_click_at"), sa.column("last_click_at"), sa.column("click_count"),
                         sa.column("demo_at"), sa.column("demo_tenant_id"), sa.column("signed_up_at"),
                         sa.column("next_action_on"), sa.column("campaign_id"), sa.column("email_step"),
                         sa.column("next_email_at"), sa.column("first_opened_at"), sa.column("replied_at"))
    events = sa.table("prospect_events", sa.column("id"), sa.column("prospect_id"), sa.column("kind"),
                      sa.column("channel"), sa.column("detail"), sa.column("actor"), sa.column("at"))
    emails = sa.table("prospect_emails", sa.column("prospect_id"))
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        bind.execute(prospects.update().where(prospects.c.id.in_(chunk)).values(
            contacted_at=None, last_contact_at=None, first_click_at=None, last_click_at=None, click_count=0,
            demo_at=None, demo_tenant_id=None, next_action_on=None, campaign_id=None, email_step=0,
            next_email_at=None, first_opened_at=None, replied_at=None))
        old = events.c.kind.in_(("CONTACT", "CLICK", "DEMO"))
        bind.execute(events.update().where(events.c.prospect_id.in_(chunk), old).values(
            kind="NOTE", detail=sa.func.left(sa.literal("Avant la remise à zéro : ", sa.String()) + sa.func.coalesce(events.c.detail, events.c.kind), 500)))
        bind.execute(emails.delete().where(emails.c.prospect_id.in_(chunk)))
    now = datetime.now(timezone.utc)
    op.bulk_insert(events, [{"id": uuid.uuid4(), "prospect_id": pid, "kind": "NOTE", "channel": None, "detail": NOTE,
                             "actor": "Bob", "at": now} for pid in ids])


def downgrade() -> None:
    # La remise à zéro des prospects ne se défait pas (l'historique en garde la trace).
    op.drop_column("prospection_settings", "restaurants_changed_by")
    op.drop_column("prospection_settings", "restaurants_changed_at")
    op.drop_column("prospection_settings", "restaurants_enabled")
