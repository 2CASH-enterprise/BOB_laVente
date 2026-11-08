"""Lot 114 — formulaire WhatsApp « Réserver une table » : formulaires publiés par restaurant, interrupteurs.

Revision ID: c14d6a9b3f52
Revises: c13b5f8a2e41
Create Date: 2026-11-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'c14d6a9b3f52'
down_revision = 'c13b5f8a2e41'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'whatsapp_flows',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('key', sa.String(length=32), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=True),
        sa.Column('meta_id', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='NOT_CREATED', nullable=False),
        sa.Column('reason', sa.String(length=300), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'key', name='uq_whatsapp_flows_tenant_key'),
    )
    op.create_index(op.f('ix_whatsapp_flows_tenant_id'), 'whatsapp_flows', ['tenant_id'], unique=False)
    op.add_column('platform_settings', sa.Column('wa_flows_enabled', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('tenants', sa.Column('wa_flows', sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column('tenants', 'wa_flows')
    op.drop_column('platform_settings', 'wa_flows_enabled')
    op.drop_index(op.f('ix_whatsapp_flows_tenant_id'), table_name='whatsapp_flows')
    op.drop_table('whatsapp_flows')
