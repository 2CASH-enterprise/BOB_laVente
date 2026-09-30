"""lot 35 rendez-vous cote prospect : rappel WhatsApp, deplacement et annulation par le client

Revision ID: c35e8b1d4f27
Revises: b34d2a6f8c10
Create Date: 2026-09-30 16:20:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'c35e8b1d4f27'
down_revision = 'b34d2a6f8c10'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('appointment_requests', sa.Column('customer_whatsapp_reminder_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('appointment_requests', sa.Column('cancelled_by', sa.String(length=64), nullable=True))
    op.add_column('appointment_requests', sa.Column('rescheduled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('appointment_requests', sa.Column('rescheduled_by', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('appointment_requests', 'rescheduled_by')
    op.drop_column('appointment_requests', 'rescheduled_at')
    op.drop_column('appointment_requests', 'cancelled_by')
    op.drop_column('appointment_requests', 'customer_whatsapp_reminder_sent_at')
