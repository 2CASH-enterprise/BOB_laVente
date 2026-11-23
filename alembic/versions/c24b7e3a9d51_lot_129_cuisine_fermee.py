"""Lot 129 — « Fermer / rouvrir la cuisine » depuis le menu de Bob sur WhatsApp (jour de fermeture du restaurant).

Revision ID: c24b7e3a9d51
Revises: c21f8b4d6a29
Create Date: 2026-11-23
"""
from alembic import op
import sqlalchemy as sa

revision = 'c24b7e3a9d51'
down_revision = 'c21f8b4d6a29'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('kitchen_closed_on', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('tenants', 'kitchen_closed_on')
