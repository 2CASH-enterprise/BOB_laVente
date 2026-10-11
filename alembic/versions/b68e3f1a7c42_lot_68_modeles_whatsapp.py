"""lot 68 : modèles WhatsApp (interrupteur, modèles par boutique, page « Payer ma commande », livraison prévenue)

Revision ID: b68e3f1a7c42
Revises: a67d2e8c4f19
Create Date: 2026-10-11 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b68e3f1a7c42'
down_revision = 'a67d2e8c4f19'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('platform_settings', sa.Column('wa_templates_enabled', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('tenants', sa.Column('wa_templates_allowed', sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column('orders', sa.Column('pay_code', sa.String(length=16), nullable=True))
    op.create_unique_constraint('uq_orders_pay_code', 'orders', ['pay_code'])
    op.add_column('deliveries', sa.Column('shipped_notified_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('deliveries', sa.Column('delivered_notified_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'whatsapp_templates',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('key', sa.String(length=40), nullable=False),
        sa.Column('form', sa.String(length=8), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('meta_id', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='NOT_CREATED', nullable=False),
        sa.Column('category', sa.String(length=16), nullable=True),
        sa.Column('reason', sa.String(length=300), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_whatsapp_templates_tenant_name'),
    )
    op.create_index(op.f('ix_whatsapp_templates_tenant_id'), 'whatsapp_templates', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_whatsapp_templates_tenant_id'), table_name='whatsapp_templates')
    op.drop_table('whatsapp_templates')
    op.drop_column('deliveries', 'delivered_notified_at')
    op.drop_column('deliveries', 'shipped_notified_at')
    op.drop_constraint('uq_orders_pay_code', 'orders', type_='unique')
    op.drop_column('orders', 'pay_code')
    op.drop_column('tenants', 'wa_templates_allowed')
    op.drop_column('platform_settings', 'wa_templates_enabled')
