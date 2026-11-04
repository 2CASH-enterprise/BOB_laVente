"""Lot 106 — secteur Restaurant : couverts d'une réservation, table et commune d'une commande, sur place, frais de
livraison par commune, horaires de la cuisine.

Revision ID: c06a1f2e9b37
Revises: b95e3c7a2d48
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'c06a1f2e9b37'
down_revision = 'b95e3c7a2d48'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('appointment_requests', sa.Column('party_size', sa.Integer(), nullable=True))
    op.add_column('orders', sa.Column('table_number', sa.String(16), nullable=True))
    op.add_column('orders', sa.Column('delivery_area', sa.String(64), nullable=True))
    op.add_column('tenants', sa.Column('dine_in_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('tenants', sa.Column('delivery_areas', sa.JSON(), nullable=True))
    op.add_column('tenants', sa.Column('kitchen_hours', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('tenants', 'kitchen_hours')
    op.drop_column('tenants', 'delivery_areas')
    op.drop_column('tenants', 'dine_in_enabled')
    op.drop_column('orders', 'delivery_area')
    op.drop_column('orders', 'table_number')
    op.drop_column('appointment_requests', 'party_size')
