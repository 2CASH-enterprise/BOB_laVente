"""Lot 95 — panier sur WhatsApp, livraison ou retrait, taille sur la ligne de commande, frais de livraison.

Revision ID: b95e3c7a2d48
Revises: a94d2f6b8c15
Create Date: 2026-10-31
"""
from alembic import op
import sqlalchemy as sa

revision = 'b95e3c7a2d48'
down_revision = 'a94d2f6b8c15'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('delivery_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('tenants', sa.Column('delivery_fee', sa.Numeric(14, 2), nullable=True))
    op.add_column('tenants', sa.Column('delivery_zone', sa.String(200), nullable=True))
    op.add_column('tenants', sa.Column('delivery_min_order', sa.Numeric(14, 2), nullable=True))
    op.add_column('tenants', sa.Column('delivery_free_from', sa.Numeric(14, 2), nullable=True))
    op.add_column('tenants', sa.Column('pickup_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('tenants', sa.Column('pickup_address', sa.String(300), nullable=True))

    op.add_column('orders', sa.Column('delivery_fee', sa.Numeric(14, 2), nullable=True))
    op.add_column('orders', sa.Column('fulfillment', sa.String(16), nullable=True))
    op.add_column('order_items', sa.Column('variant_index', sa.Integer(), nullable=True))
    op.add_column('order_items', sa.Column('variant_label', sa.String(120), nullable=True))

    op.create_table(
        'carts',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('tenant_id', sa.Uuid(), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', sa.Uuid(), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('items', sa.JSON(), nullable=True),
        sa.Column('fulfillment', sa.String(16), nullable=True),
        sa.Column('address', sa.String(500), nullable=True),
        sa.Column('awaiting', sa.String(120), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('tenant_id', 'customer_id', name='uq_carts_tenant_customer'),
    )
    op.create_index('ix_carts_updated_at', 'carts', ['updated_at'])


def downgrade() -> None:
    op.drop_index('ix_carts_updated_at', table_name='carts')
    op.drop_table('carts')
    op.drop_column('order_items', 'variant_label')
    op.drop_column('order_items', 'variant_index')
    op.drop_column('orders', 'fulfillment')
    op.drop_column('orders', 'delivery_fee')
    for column in ('pickup_address', 'pickup_enabled', 'delivery_free_from', 'delivery_min_order', 'delivery_zone',
                   'delivery_fee', 'delivery_enabled'):
        op.drop_column('tenants', column)
