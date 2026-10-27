"""Lot 92 — « Bob, votre assistant » sur le numéro AgenC'AI : portée, interrupteur, demandes d'appairage, messages.

Revision ID: e92b4c7d1f36
Revises: d90a3c7e5b12
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = 'e92b4c7d1f36'
down_revision = 'd90a3c7e5b12'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('naomy_connections', sa.Column('scope', sa.String(length=10), nullable=False, server_default='complete'))
    op.add_column('naomy_connections', sa.Column('assistant_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('naomy_connections', sa.Column('assistant_enabled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('naomy_connections', sa.Column('summary_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('naomy_connections', sa.Column('last_summary_on', sa.Date(), nullable=True))
    op.add_column('naomy_connections', sa.Column('awaiting', sa.String(length=80), nullable=True))
    op.add_column('naomy_connections', sa.Column('awaiting_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('naomy_pairing_codes', sa.Column('scope', sa.String(length=10), nullable=False, server_default='complete'))
    op.create_table(
        'naomy_pairing_requests',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('naomy_account', sa.String(length=64), nullable=False),
        sa.Column('shop_name', sa.String(length=120), nullable=True),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_naomy_pairing_requests_tenant_id', 'naomy_pairing_requests', ['tenant_id'])
    op.create_table(
        'assistant_messages',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('connection_id', sa.Uuid(), nullable=False),
        sa.Column('wa_message_id', sa.String(length=128), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('button_id', sa.String(length=256), nullable=True),
        sa.Column('media_path', sa.String(length=300), nullable=True),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['connection_id'], ['naomy_connections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('connection_id', 'wa_message_id', name='uq_assistant_message'),
    )
    op.create_index('ix_assistant_messages_tenant_id', 'assistant_messages', ['tenant_id'])
    op.create_index('ix_assistant_messages_status', 'assistant_messages', ['status'])
    op.create_table(
        'assistant_outbox',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('connection_id', sa.Uuid(), nullable=False),
        sa.Column('message_id', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('ref', sa.String(length=80), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('fallback_subject', sa.String(length=200), nullable=True),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['connection_id'], ['naomy_connections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id'),
    )
    op.create_index('ix_assistant_outbox_tenant_id', 'assistant_outbox', ['tenant_id'])
    op.create_index('ix_assistant_outbox_ref', 'assistant_outbox', ['ref'])
    op.create_index('ix_assistant_outbox_status', 'assistant_outbox', ['status'])
    op.create_index('ix_assistant_outbox_created_at', 'assistant_outbox', ['created_at'])


def downgrade() -> None:
    op.drop_table('assistant_outbox')
    op.drop_table('assistant_messages')
    op.drop_index('ix_naomy_pairing_requests_tenant_id', table_name='naomy_pairing_requests')
    op.drop_table('naomy_pairing_requests')
    op.drop_column('naomy_pairing_codes', 'scope')
    for column in ('awaiting_until', 'awaiting', 'last_summary_on', 'summary_enabled', 'assistant_enabled_at',
                   'assistant_enabled', 'scope'):
        op.drop_column('naomy_connections', column)
