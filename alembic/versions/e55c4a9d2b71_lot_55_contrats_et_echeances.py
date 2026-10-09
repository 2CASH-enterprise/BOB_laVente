"""lot 55 courtier : registre des contrats, échéances, suivi des demandes de cotation

Revision ID: e55c4a9d2b71
Revises: d54b3f8a2c17
Create Date: 2026-10-09 17:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e55c4a9d2b71'
down_revision = 'd54b3f8a2c17'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'insurance_contracts',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('customer_id', sa.Uuid(), nullable=False),
        sa.Column('quote_request_id', sa.Uuid(), nullable=True),
        sa.Column('renewed_from_id', sa.Uuid(), nullable=True),
        sa.Column('branch', sa.String(length=32), nullable=False),
        sa.Column('insurer', sa.String(length=150), nullable=True),
        sa.Column('policy_number', sa.String(length=64), nullable=True),
        sa.Column('premium', sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('effective_on', sa.Date(), nullable=True),
        sa.Column('expires_on', sa.Date(), nullable=False),
        sa.Column('term', sa.String(length=16), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='ACTIVE', nullable=False),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('broker_alerted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('client_reminded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('client_reminder_channel', sa.String(length=16), nullable=True),
        sa.Column('renewal_handled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('renewal_handled_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['quote_request_id'], ['quote_requests.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['renewed_from_id'], ['insurance_contracts.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_insurance_contracts_tenant_id'), 'insurance_contracts', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_insurance_contracts_customer_id'), 'insurance_contracts', ['customer_id'], unique=False)
    op.create_index(op.f('ix_insurance_contracts_status'), 'insurance_contracts', ['status'], unique=False)
    op.create_index('ix_insurance_contracts_tenant_expiry', 'insurance_contracts', ['tenant_id', 'expires_on'], unique=False)

    op.add_column('tenants', sa.Column('renewal_reminders_enabled', sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column('tenants', sa.Column('renewal_digest_on', sa.Date(), nullable=True))

    op.add_column('quote_requests', sa.Column('proposal_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('quote_requests', sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('quote_requests', sa.Column('lost_reason', sa.String(length=150), nullable=True))


def downgrade() -> None:
    op.drop_column('quote_requests', 'lost_reason')
    op.drop_column('quote_requests', 'closed_at')
    op.drop_column('quote_requests', 'proposal_sent_at')
    op.drop_column('tenants', 'renewal_digest_on')
    op.drop_column('tenants', 'renewal_reminders_enabled')
    op.drop_index('ix_insurance_contracts_tenant_expiry', table_name='insurance_contracts')
    op.drop_index(op.f('ix_insurance_contracts_status'), table_name='insurance_contracts')
    op.drop_index(op.f('ix_insurance_contracts_customer_id'), table_name='insurance_contracts')
    op.drop_index(op.f('ix_insurance_contracts_tenant_id'), table_name='insurance_contracts')
    op.drop_table('insurance_contracts')
