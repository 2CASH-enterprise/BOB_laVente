"""lot 65 : pré-inscription (mode de la plateforme, comptes pré-inscrits, offre de lancement, fichiers préparés)

Revision ID: f65a2c7d9e31
Revises: e63b4d9a2c58
Create Date: 2026-10-11 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f65a2c7d9e31'
down_revision = 'e63b4d9a2c58'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('prereg_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tenants', sa.Column('access_opened_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tenants', sa.Column('launch_offer', sa.String(length=32), nullable=True))
    op.add_column('tenants', sa.Column('prep_whatsapp', sa.String(length=40), nullable=True))
    op.add_column('tenants', sa.Column('prep_catalog_size', sa.String(length=16), nullable=True))
    op.add_column('tenants', sa.Column('prep_note', sa.String(length=500), nullable=True))
    op.add_column('tenants', sa.Column('prep_updated_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'platform_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('prereg_mode', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('prereg_changed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('prereg_changed_by', sa.String(length=120), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'preregistration_files',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('filename', sa.String(length=200), nullable=False),
        sa.Column('content_type', sa.String(length=120), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_preregistration_files_tenant_id'), 'preregistration_files', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_preregistration_files_tenant_id'), table_name='preregistration_files')
    op.drop_table('preregistration_files')
    op.drop_table('platform_settings')
    for column in ('prep_updated_at', 'prep_note', 'prep_catalog_size', 'prep_whatsapp', 'launch_offer',
                   'access_opened_at', 'prereg_at'):
        op.drop_column('tenants', column)
