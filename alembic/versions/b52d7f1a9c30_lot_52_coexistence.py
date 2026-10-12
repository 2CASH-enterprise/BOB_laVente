"""lot 52 coexistence (historique et contacts du telephone)

Revision ID: b52d7f1a9c30
Revises: d70b5e2c9a14 (recalée le 12/10 après le lot 70)
Create Date: 2026-10-03 19:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b52d7f1a9c30'
down_revision = 'd70b5e2c9a14'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('phone_contacts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('wa_number', sa.String(length=32), nullable=False),
    sa.Column('full_name', sa.String(length=255), nullable=True),
    sa.Column('first_name', sa.String(length=255), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'wa_number', name='uq_phone_contacts_tenant_number')
    )
    op.create_index(op.f('ix_phone_contacts_tenant_id'), 'phone_contacts', ['tenant_id'], unique=False)
    op.create_table('phone_history_messages',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('wa_number', sa.String(length=32), nullable=False),
    sa.Column('from_business', sa.Boolean(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('wa_message_id', sa.String(length=128), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'wa_message_id', name='uq_phone_history_tenant_wamid')
    )
    op.create_index('ix_phone_history_tenant_number_sent', 'phone_history_messages', ['tenant_id', 'wa_number', 'sent_at'], unique=False)
    op.add_column('whatsapp_accounts', sa.Column('contacts_sync_requested_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('whatsapp_accounts', sa.Column('history_sync_requested_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('whatsapp_accounts', sa.Column('history_sync_status', sa.String(length=16), nullable=True))
    op.add_column('whatsapp_accounts', sa.Column('history_progress', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('whatsapp_accounts', 'history_progress')
    op.drop_column('whatsapp_accounts', 'history_sync_status')
    op.drop_column('whatsapp_accounts', 'history_sync_requested_at')
    op.drop_column('whatsapp_accounts', 'contacts_sync_requested_at')
    op.drop_index('ix_phone_history_tenant_number_sent', table_name='phone_history_messages')
    op.drop_table('phone_history_messages')
    op.drop_index(op.f('ix_phone_contacts_tenant_id'), table_name='phone_contacts')
    op.drop_table('phone_contacts')
