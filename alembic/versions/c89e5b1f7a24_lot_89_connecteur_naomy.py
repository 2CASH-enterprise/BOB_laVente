"""Lot 89 — connecteur Naomy : liaisons, codes d'appairage, événements, rappels ; liens suivis créés par Naomy.

Revision ID: c89e5b1f7a24
Revises: b88d4f2a6c13
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa

revision = 'c89e5b1f7a24'
down_revision = 'b88d4f2a6c13'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'naomy_connections',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('naomy_account', sa.String(length=64), nullable=False),
        sa.Column('confirmation_number', sa.String(length=20), nullable=False),
        sa.Column('events_url', sa.String(length=500), nullable=False),
        sa.Column('api_key_hash', sa.String(length=64), nullable=False),
        sa.Column('events_secret', sa.String(length=512), nullable=False),
        sa.Column('reminders_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('reminders_unsubscribed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('connected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closed_by', sa.String(length=8), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_naomy_connections_tenant_id', 'naomy_connections', ['tenant_id'])
    op.create_index('ix_naomy_connections_api_key_hash', 'naomy_connections', ['api_key_hash'], unique=True)
    op.create_table(
        'naomy_pairing_codes',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code_hash'),
    )
    op.create_index('ix_naomy_pairing_codes_tenant_id', 'naomy_pairing_codes', ['tenant_id'])
    op.create_table(
        'naomy_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('connection_id', sa.Uuid(), nullable=True),
        sa.Column('type', sa.String(length=40), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('product_id', sa.Uuid(), nullable=True),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['connection_id'], ['naomy_connections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_naomy_events_tenant_id', 'naomy_events', ['tenant_id'])
    op.create_index('ix_naomy_events_product_id', 'naomy_events', ['product_id'])
    op.create_index('ix_naomy_events_status', 'naomy_events', ['status'])
    op.create_index('ix_naomy_events_created_at', 'naomy_events', ['created_at'])
    op.create_table(
        'naomy_reminders',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('reference', sa.String(length=64), nullable=False),
        sa.Column('sent_on', sa.Date(), nullable=False),
        sa.Column('sent', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'reference', name='uq_naomy_reminder_reference'),
    )
    op.create_index('ix_naomy_reminders_tenant_id', 'naomy_reminders', ['tenant_id'])
    op.add_column('contact_points', sa.Column('created_by', sa.String(length=16), nullable=True))
    op.add_column('contact_points', sa.Column('naomy_reference', sa.String(length=64), nullable=True))
    op.add_column('contact_points', sa.Column('product_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_contact_points_product_id', 'contact_points', 'products', ['product_id'], ['id'],
                          ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_contact_points_product_id', 'contact_points', type_='foreignkey')
    op.drop_column('contact_points', 'product_id')
    op.drop_column('contact_points', 'naomy_reference')
    op.drop_column('contact_points', 'created_by')
    op.drop_index('ix_naomy_reminders_tenant_id', table_name='naomy_reminders')
    op.drop_table('naomy_reminders')
    for name in ('created_at', 'status', 'product_id', 'tenant_id'):
        op.drop_index(f'ix_naomy_events_{name}', table_name='naomy_events')
    op.drop_table('naomy_events')
    op.drop_index('ix_naomy_pairing_codes_tenant_id', table_name='naomy_pairing_codes')
    op.drop_table('naomy_pairing_codes')
    op.drop_index('ix_naomy_connections_api_key_hash', table_name='naomy_connections')
    op.drop_index('ix_naomy_connections_tenant_id', table_name='naomy_connections')
    op.drop_table('naomy_connections')
