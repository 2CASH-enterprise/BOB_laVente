"""lot 34 email et consentement du client final

Revision ID: b34d2a6f8c10
Revises: a30c7e2f4b18
Create Date: 2026-09-30 14:40:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'b34d2a6f8c10'
down_revision = 'a30c7e2f4b18'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('customers', sa.Column('email_source', sa.String(length=32), nullable=True))
    op.add_column('customers', sa.Column('email_collected_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('customers', sa.Column('email_requested_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('customers', 'email_requested_at')
    op.drop_column('customers', 'email_collected_at')
    op.drop_column('customers', 'email_source')
