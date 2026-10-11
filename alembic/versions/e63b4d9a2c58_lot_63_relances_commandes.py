"""lot 63 : relances des commandes non payées (étapes, dates, canal) et réglage de la boutique

Revision ID: e63b4d9a2c58
Revises: d62a8c3f5b17
Create Date: 2026-10-11 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e63b4d9a2c58'
down_revision = 'd62a8c3f5b17'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('reminders_sent', sa.Integer(), server_default='0', nullable=False))
    op.add_column('orders', sa.Column('first_reminder_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('last_reminder_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('last_reminder_channel', sa.String(length=16), nullable=True))
    op.add_column('tenants', sa.Column('order_reminders_enabled', sa.Boolean(), server_default=sa.true(), nullable=False))


def downgrade() -> None:
    op.drop_column('tenants', 'order_reminders_enabled')
    op.drop_column('orders', 'last_reminder_channel')
    op.drop_column('orders', 'last_reminder_at')
    op.drop_column('orders', 'first_reminder_at')
    op.drop_column('orders', 'reminders_sent')
