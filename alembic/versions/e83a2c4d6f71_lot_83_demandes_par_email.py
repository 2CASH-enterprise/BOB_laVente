"""lot 83 demandes reçues par email (portails immobiliers, formulaire du site)

Revision ID: e83a2c4d6f71
Revises: d82f1a3b6c54
Create Date: 2026-10-18 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e83a2c4d6f71'
down_revision = 'd82f1a3b6c54'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('lead_inbox_code', sa.String(length=32), nullable=True))
    op.create_unique_constraint('uq_tenants_lead_inbox_code', 'tenants', ['lead_inbox_code'])
    op.add_column('tenants', sa.Column('lead_auto_send', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('tenants', sa.Column('lead_gmail_code', sa.String(length=32), nullable=True))
    op.add_column('tenants', sa.Column('lead_gmail_code_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'inbound_leads',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('message_id', sa.String(length=255), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=True),
        sa.Column('contact_name', sa.String(length=200), nullable=True),
        sa.Column('contact_email', sa.String(length=255), nullable=True),
        sa.Column('contact_phone', sa.String(length=32), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('reference', sa.String(length=64), nullable=True),
        sa.Column('product_id', sa.Uuid(), nullable=True),
        sa.Column('customer_id', sa.Uuid(), nullable=True),
        sa.Column('reply_subject', sa.String(length=255), nullable=True),
        sa.Column('reply_text', sa.Text(), nullable=True),
        sa.Column('reason', sa.String(length=300), nullable=True),
        sa.Column('is_test', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('replied_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_by', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'message_id', name='uq_inbound_lead_message'),
    )
    op.create_index('ix_inbound_leads_tenant_id', 'inbound_leads', ['tenant_id'])
    op.create_index('ix_inbound_leads_status', 'inbound_leads', ['status'])
    op.create_table(
        'lead_inbox_state',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('last_uid', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(length=300), nullable=True),
        sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('lead_inbox_state')
    op.drop_index('ix_inbound_leads_status', table_name='inbound_leads')
    op.drop_index('ix_inbound_leads_tenant_id', table_name='inbound_leads')
    op.drop_table('inbound_leads')
    op.drop_column('tenants', 'lead_gmail_code_at')
    op.drop_column('tenants', 'lead_gmail_code')
    op.drop_column('tenants', 'lead_auto_send')
    op.drop_constraint('uq_tenants_lead_inbox_code', 'tenants', type_='unique')
    op.drop_column('tenants', 'lead_inbox_code')
