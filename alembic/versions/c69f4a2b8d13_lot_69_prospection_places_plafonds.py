"""lot 69 : places de l'offre de lancement, plafonds des formules, rythme WhatsApp de la prospection

Revision ID: c69f4a2b8d13
Revises: b68e3f1a7c42
Create Date: 2026-10-11 21:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c69f4a2b8d13'
down_revision = 'b68e3f1a7c42'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('platform_settings', sa.Column('launch_slots', sa.Integer(), server_default='100', nullable=False))
    op.add_column('platform_settings', sa.Column('plan_quotas', sa.JSON(), nullable=True))
    op.add_column('platform_settings', sa.Column('quota_mode', sa.String(length=8), server_default='WARN', nullable=False))
    op.add_column('prospection_settings', sa.Column('whatsapp_daily_limit', sa.Integer(), server_default='25', nullable=False))
    op.add_column('tenants', sa.Column('quota_alert_month', sa.String(length=7), nullable=True))
    op.add_column('tenants', sa.Column('quota_alert_level', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('tenants', 'quota_alert_level')
    op.drop_column('tenants', 'quota_alert_month')
    op.drop_column('prospection_settings', 'whatsapp_daily_limit')
    op.drop_column('platform_settings', 'quota_mode')
    op.drop_column('platform_settings', 'plan_quotas')
    op.drop_column('platform_settings', 'launch_slots')
