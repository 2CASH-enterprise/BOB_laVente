"""lot 57 courtier : registre des réclamations et des sinistres, délai de réponse

Revision ID: f57a3d8c1e94
Revises: e55c4a9d2b71
Create Date: 2026-10-09 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f57a3d8c1e94'
down_revision = 'e55c4a9d2b71'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'insurance_complaints',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('customer_id', sa.Uuid(), nullable=False),
        sa.Column('conversation_id', sa.Uuid(), nullable=True),
        sa.Column('reference', sa.String(length=20), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('channel', sa.String(length=16), nullable=False),
        sa.Column('subject', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), server_default='RECEIVED', nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('due_on', sa.Date(), nullable=True),
        sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('follow_ups', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('started_by', sa.String(length=64), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by', sa.String(length=64), nullable=True),
        sa.Column('resolution', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'reference', name='uq_insurance_complaints_reference'),
    )
    op.create_index(op.f('ix_insurance_complaints_tenant_id'), 'insurance_complaints', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_insurance_complaints_customer_id'), 'insurance_complaints', ['customer_id'], unique=False)
    op.create_index(op.f('ix_insurance_complaints_status'), 'insurance_complaints', ['status'], unique=False)
    op.add_column('tenants', sa.Column('complaint_delay_days', sa.Integer(), server_default='10', nullable=False))


def downgrade() -> None:
    op.drop_column('tenants', 'complaint_delay_days')
    op.drop_index(op.f('ix_insurance_complaints_status'), table_name='insurance_complaints')
    op.drop_index(op.f('ix_insurance_complaints_customer_id'), table_name='insurance_complaints')
    op.drop_index(op.f('ix_insurance_complaints_tenant_id'), table_name='insurance_complaints')
    op.drop_table('insurance_complaints')
