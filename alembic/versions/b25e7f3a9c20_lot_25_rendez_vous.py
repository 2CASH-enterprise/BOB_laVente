"""lot 25 rendez-vous : fiche de qualification, confirmation, rappels

Revision ID: b25e7f3a9c20
Revises: a24c0b0e5d11
Create Date: 2026-09-29 18:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'b25e7f3a9c20'
down_revision = 'a24c0b0e5d11'
branch_labels = None
depends_on = None

_COLUMNS = [
    ('need', sa.String(length=300)),
    ('budget', sa.String(length=100)),
    ('trade_in', sa.String(length=300)),
    ('financing_interest', sa.Boolean()),
    ('scheduled_at', sa.DateTime(timezone=True)),
    ('confirmed_at', sa.DateTime(timezone=True)),
    ('confirmed_by', sa.String(length=64)),
    ('cancelled_at', sa.DateTime(timezone=True)),
    ('reminder_sent_at', sa.DateTime(timezone=True)),
    ('customer_reminder_sent_at', sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column('appointment_requests', sa.Column(name, type_, nullable=True))
    op.create_index('ix_appointment_requests_scheduled_at', 'appointment_requests', ['scheduled_at'])


def downgrade() -> None:
    op.drop_index('ix_appointment_requests_scheduled_at', table_name='appointment_requests')
    for name, _ in reversed(_COLUMNS):
        op.drop_column('appointment_requests', name)
