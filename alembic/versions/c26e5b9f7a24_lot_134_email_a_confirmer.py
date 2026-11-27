"""Lot 134 — e-mail facultatif et à confirmer pour les comptes créés par Naomy (choix B du 27/11).

Revision ID: c26e5b9f7a24
Revises: c25d4a8e6b13
"""
import sqlalchemy as sa
from alembic import op

revision = "c26e5b9f7a24"
down_revision = "c25d4a8e6b13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("email_pending", sa.String(length=255), nullable=True))
    op.add_column("tenants", sa.Column("email_confirm_hash", sa.String(length=64), nullable=True))
    op.add_column("tenants", sa.Column("email_confirm_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "email_confirm_sent_at")
    op.drop_column("tenants", "email_confirm_hash")
    op.drop_column("tenants", "email_pending")
