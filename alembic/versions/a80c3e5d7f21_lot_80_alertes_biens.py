"""lot 80 alertes nouveau bien (agence immobiliere)

Revision ID: a80c3e5d7f21
Revises: f79b2d4e6a10
Create Date: 2026-10-17 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a80c3e5d7f21'
down_revision = 'f79b2d4e6a10'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('property_alerts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('product_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.String(length=16), nullable=False),
    sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('sent_by', sa.String(length=64), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_property_alerts_customer_id'), 'property_alerts', ['customer_id'], unique=False)
    op.create_index(op.f('ix_property_alerts_product_id'), 'property_alerts', ['product_id'], unique=False)
    op.create_index(op.f('ix_property_alerts_tenant_id'), 'property_alerts', ['tenant_id'], unique=False)
    op.add_column('tenants', sa.Column('property_alerts_auto', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('tenants', sa.Column('property_alert_templates', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('tenants', 'property_alert_templates')
    op.drop_column('tenants', 'property_alerts_auto')
    op.drop_index(op.f('ix_property_alerts_tenant_id'), table_name='property_alerts')
    op.drop_index(op.f('ix_property_alerts_product_id'), table_name='property_alerts')
    op.drop_index(op.f('ix_property_alerts_customer_id'), table_name='property_alerts')
    op.drop_table('property_alerts')
