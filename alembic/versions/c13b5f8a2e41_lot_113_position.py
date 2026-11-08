"""Lot 113 — position de livraison partagée sur WhatsApp (client : dernière position ; commande : point exact).

Revision ID: c13b5f8a2e41
Revises: c12a4e7b9d30
Create Date: 2026-11-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'c13b5f8a2e41'
down_revision = 'c12a4e7b9d30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('customers', sa.Column('delivery_location', sa.JSON(), nullable=True))
    op.add_column('orders', sa.Column('delivery_location', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'delivery_location')
    op.drop_column('customers', 'delivery_location')
