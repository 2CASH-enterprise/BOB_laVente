"""lot 24 mode concession : type d'activite, demandes de rendez-vous

Revision ID: a24c0b0e5d11
Revises: 066aaad3e12e
Create Date: 2026-09-29 15:30:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'a24c0b0e5d11'
down_revision = '066aaad3e12e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('business_type', sa.String(length=32), server_default='ONLINE_STORE', nullable=False))
    op.add_column('tenants', sa.Column('business_type_chosen_at', sa.DateTime(timezone=True), nullable=True))
    # Les boutiques existantes ont déjà leur fonctionnement : on ne leur montre pas l'écran de choix.
    op.execute("UPDATE tenants SET business_type_chosen_at = now()")
    op.create_table('appointment_requests',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('product_id', sa.Uuid(), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('vehicle_label', sa.String(length=255), nullable=True),
    sa.Column('availability', sa.String(length=300), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_appointment_requests_tenant_id', 'appointment_requests', ['tenant_id'])
    op.create_index('ix_appointment_requests_conversation_id', 'appointment_requests', ['conversation_id'])
    op.create_index('ix_appointment_requests_customer_id', 'appointment_requests', ['customer_id'])


def downgrade() -> None:
    op.drop_index('ix_appointment_requests_customer_id', table_name='appointment_requests')
    op.drop_index('ix_appointment_requests_conversation_id', table_name='appointment_requests')
    op.drop_index('ix_appointment_requests_tenant_id', table_name='appointment_requests')
    op.drop_table('appointment_requests')
    op.drop_column('tenants', 'business_type_chosen_at')
    op.drop_column('tenants', 'business_type')
