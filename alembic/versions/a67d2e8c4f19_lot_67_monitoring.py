"""lot 67 : monitoring technique (erreurs regroupées et leurs occurrences)

Revision ID: a67d2e8c4f19
Revises: f65a2c7d9e31
Create Date: 2026-10-11 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a67d2e8c4f19'
down_revision = 'f65a2c7d9e31'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'monitor_issues',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('signature', sa.String(length=40), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('exc_type', sa.String(length=120), nullable=True),
        sa.Column('location', sa.String(length=300), nullable=True),
        sa.Column('context', sa.String(length=300), nullable=True),
        sa.Column('meta_code', sa.String(length=16), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='NEW', nullable=False),
        sa.Column('count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_message', sa.String(length=1000), nullable=True),
        sa.Column('last_trace', sa.Text(), nullable=True),
        sa.Column('tenant_ids', sa.JSON(), nullable=True),
        sa.Column('browsers', sa.JSON(), nullable=True),
        sa.Column('assignee', sa.String(length=255), nullable=True),
        sa.Column('status_changed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by', sa.String(length=255), nullable=True),
        sa.Column('reopened_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('alerted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('spike_alerted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('signature'),
    )
    op.create_index(op.f('ix_monitor_issues_source'), 'monitor_issues', ['source'], unique=False)
    op.create_index(op.f('ix_monitor_issues_status'), 'monitor_issues', ['status'], unique=False)
    op.create_index(op.f('ix_monitor_issues_last_seen'), 'monitor_issues', ['last_seen'], unique=False)
    op.create_table(
        'monitor_occurrences',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('issue_id', sa.Uuid(), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=True),
        sa.Column('message', sa.String(length=500), nullable=True),
        sa.Column('context', sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(['issue_id'], ['monitor_issues.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_monitor_occurrences_issue_id'), 'monitor_occurrences', ['issue_id'], unique=False)
    op.create_index(op.f('ix_monitor_occurrences_at'), 'monitor_occurrences', ['at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_monitor_occurrences_at'), table_name='monitor_occurrences')
    op.drop_index(op.f('ix_monitor_occurrences_issue_id'), table_name='monitor_occurrences')
    op.drop_table('monitor_occurrences')
    op.drop_index(op.f('ix_monitor_issues_last_seen'), table_name='monitor_issues')
    op.drop_index(op.f('ix_monitor_issues_status'), table_name='monitor_issues')
    op.drop_index(op.f('ix_monitor_issues_source'), table_name='monitor_issues')
    op.drop_table('monitor_issues')
