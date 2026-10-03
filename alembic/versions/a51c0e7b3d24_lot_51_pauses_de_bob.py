"""lot 51 pauses de bob (coexistence, abonnement)

Revision ID: a51c0e7b3d24
Revises: f50a3c9e1b72
Create Date: 2026-10-03 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a51c0e7b3d24'
down_revision = 'f50a3c9e1b72'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('conversations', sa.Column('phone_reply_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tenants', sa.Column('paid_until', sa.Date(), nullable=True))
    op.add_column('tenants', sa.Column('billing_notice_stage', sa.String(length=16), nullable=True))
    op.add_column('tenants', sa.Column('billing_notice_for', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('tenants', 'billing_notice_for')
    op.drop_column('tenants', 'billing_notice_stage')
    op.drop_column('tenants', 'paid_until')
    op.drop_column('conversations', 'phone_reply_at')
