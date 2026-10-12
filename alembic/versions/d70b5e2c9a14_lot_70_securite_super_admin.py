"""lot 70 : code de connexion Super Admin (double vérification par email)

Revision ID: d70b5e2c9a14
Revises: c69f4a2b8d13
Create Date: 2026-10-12 07:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd70b5e2c9a14'
down_revision = 'c69f4a2b8d13'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('superadmin_users', sa.Column('mfa_otp_code_hash', sa.String(length=64), nullable=True))
    op.add_column('superadmin_users', sa.Column('mfa_otp_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('superadmin_users', sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('superadmin_users', 'last_login_at')
    op.drop_column('superadmin_users', 'mfa_otp_expires_at')
    op.drop_column('superadmin_users', 'mfa_otp_code_hash')
