"""lot 53 courtier d'assurance (demandes de cotation)

Revision ID: c53a8e2f4b61
Revises: a51c0e7b3d24
Create Date: 2026-10-09 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c53a8e2f4b61'
down_revision = 'a51c0e7b3d24'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('quote_requests',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=True),
    sa.Column('branch', sa.String(length=32), nullable=False),
    sa.Column('client_type', sa.String(length=16), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('handled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('handled_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_quote_requests_customer_id'), 'quote_requests', ['customer_id'], unique=False)
    op.create_index(op.f('ix_quote_requests_status'), 'quote_requests', ['status'], unique=False)
    op.create_index(op.f('ix_quote_requests_tenant_id'), 'quote_requests', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_quote_requests_tenant_id'), table_name='quote_requests')
    op.drop_index(op.f('ix_quote_requests_status'), table_name='quote_requests')
    op.drop_index(op.f('ix_quote_requests_customer_id'), table_name='quote_requests')
    op.drop_table('quote_requests')
