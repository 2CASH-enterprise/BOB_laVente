"""Lot 107 — parcours à boutons du restaurant : choix du panier (commune, table, paiement, heure) et commande pour plus
tard.

Revision ID: c07b2e4d6a19
Revises: c06a1f2e9b37
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'c07b2e4d6a19'
down_revision = 'c06a1f2e9b37'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('carts', sa.Column('meta', sa.JSON(), nullable=True))
    op.add_column('orders', sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'scheduled_for')
    op.drop_column('carts', 'meta')
