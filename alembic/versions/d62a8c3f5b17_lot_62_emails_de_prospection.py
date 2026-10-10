"""lot 62 : emails de prospection (campagnes, envois, désinscriptions, réglages)

Revision ID: d62a8c3f5b17
Revises: c61f2a9b7e30
Create Date: 2026-10-10 16:35:16.926648

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd62a8c3f5b17'
down_revision = 'c61f2a9b7e30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('prospect_campaigns',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('sector', sa.String(length=32), nullable=True),
    sa.Column('source', sa.String(length=120), nullable=True),
    sa.Column('country', sa.String(length=2), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='DRAFT', nullable=False),
    sa.Column('subject_1', sa.String(length=200), nullable=False),
    sa.Column('body_1', sa.Text(), nullable=False),
    sa.Column('subject_2', sa.String(length=200), nullable=True),
    sa.Column('body_2', sa.Text(), nullable=True),
    sa.Column('subject_3', sa.String(length=200), nullable=True),
    sa.Column('body_3', sa.Text(), nullable=True),
    sa.Column('created_by', sa.String(length=255), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('prospect_suppressions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('email_hash', sa.String(length=64), nullable=False),
    sa.Column('reason', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email_hash')
    )
    op.create_table('prospection_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('daily_limit', sa.Integer(), server_default='20', nullable=False),
    sa.Column('sending_enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('last_imap_uid', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error', sa.String(length=300), nullable=True),
    sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('prospect_emails',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('prospect_id', sa.Uuid(), nullable=False),
    sa.Column('campaign_id', sa.Uuid(), nullable=True),
    sa.Column('step', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message_id', sa.String(length=255), nullable=True),
    sa.Column('error', sa.String(length=300), nullable=True),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['prospect_campaigns.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['prospect_id'], ['prospects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_prospect_emails_campaign_id'), 'prospect_emails', ['campaign_id'], unique=False)
    op.create_index(op.f('ix_prospect_emails_message_id'), 'prospect_emails', ['message_id'], unique=False)
    op.create_index(op.f('ix_prospect_emails_prospect_id'), 'prospect_emails', ['prospect_id'], unique=False)
    op.create_index(op.f('ix_prospect_emails_sent_at'), 'prospect_emails', ['sent_at'], unique=False)
    op.add_column('prospects', sa.Column('campaign_id', sa.Uuid(), nullable=True))
    op.add_column('prospects', sa.Column('email_step', sa.Integer(), server_default='0', nullable=False))
    op.add_column('prospects', sa.Column('next_email_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('prospects', sa.Column('first_opened_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('prospects', sa.Column('replied_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_prospects_campaign_id'), 'prospects', ['campaign_id'], unique=False)
    op.create_index(op.f('ix_prospects_next_email_at'), 'prospects', ['next_email_at'], unique=False)
    op.create_foreign_key('fk_prospects_campaign_id', 'prospects', 'prospect_campaigns', ['campaign_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_prospects_campaign_id', 'prospects', type_='foreignkey')
    op.drop_index(op.f('ix_prospects_next_email_at'), table_name='prospects')
    op.drop_index(op.f('ix_prospects_campaign_id'), table_name='prospects')
    op.drop_column('prospects', 'replied_at')
    op.drop_column('prospects', 'first_opened_at')
    op.drop_column('prospects', 'next_email_at')
    op.drop_column('prospects', 'email_step')
    op.drop_column('prospects', 'campaign_id')
    op.drop_index(op.f('ix_prospect_emails_sent_at'), table_name='prospect_emails')
    op.drop_index(op.f('ix_prospect_emails_prospect_id'), table_name='prospect_emails')
    op.drop_index(op.f('ix_prospect_emails_message_id'), table_name='prospect_emails')
    op.drop_index(op.f('ix_prospect_emails_campaign_id'), table_name='prospect_emails')
    op.drop_table('prospect_emails')
    op.drop_table('prospection_settings')
    op.drop_table('prospect_suppressions')
    op.drop_table('prospect_campaigns')
