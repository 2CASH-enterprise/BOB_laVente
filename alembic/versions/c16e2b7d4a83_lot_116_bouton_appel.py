"""Lot 116 — numéro pour les appels (bouton « 📞 Appeler » sous le transfert vers la boutique).

Revision ID: c16e2b7d4a83
Revises: c14d6a9b3f52
Create Date: 2026-11-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'c16e2b7d4a83'
down_revision = 'c14d6a9b3f52'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('call_phone', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('tenants', 'call_phone')
