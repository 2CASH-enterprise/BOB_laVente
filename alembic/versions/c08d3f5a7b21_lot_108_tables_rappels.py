"""Lot 108 — QR codes des tables du restaurant, statut « Prête » d'une commande.

Revision ID: c08d3f5a7b21
Revises: c07b2e4d6a19
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = 'c08d3f5a7b21'
down_revision = 'c07b2e4d6a19'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'table_qr_codes',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('label', sa.String(length=8), nullable=False),
        sa.Column('scan_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_table_qr_code'),
        sa.UniqueConstraint('tenant_id', 'label', name='uq_table_qr_label'),
    )
    op.create_index(op.f('ix_table_qr_codes_tenant_id'), 'table_qr_codes', ['tenant_id'], unique=False)
    op.add_column('deliveries', sa.Column('ready_notified_at', sa.DateTime(timezone=True), nullable=True))
    # ALTER TYPE … ADD VALUE n'est pas détecté par autogenerate : écrit à la main (comme ee1bce100fac).
    op.execute("ALTER TYPE delivery_status ADD VALUE IF NOT EXISTS 'READY'")


def downgrade() -> None:
    # PostgreSQL ne retire pas une valeur d'un ENUM : « READY » reste, les commandes « Prête » repassent « En préparation ».
    op.execute("UPDATE deliveries SET status = 'PENDING' WHERE status = 'READY'")
    op.drop_column('deliveries', 'ready_notified_at')
    op.drop_index(op.f('ix_table_qr_codes_tenant_id'), table_name='table_qr_codes')
    op.drop_table('table_qr_codes')
