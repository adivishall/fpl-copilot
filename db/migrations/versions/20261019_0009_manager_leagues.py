"""manager leagues

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19 02:30:00+00:00

M1.8 mini-league room: one row per (manager, league) holding the fetched standings and the
nearest rivals' public picks, the outlook computed on the serving forecast, and the refresh
status. Keyed by the importing manager only, so export and erasure follow the manager and never
another user's FPL id. Downgrading drops the table (the data is re-fetchable public data).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('manager_leagues',
    sa.Column('manager_key', sa.String(length=64), nullable=False),
    sa.Column('league_id', sa.Integer(), nullable=False),
    sa.Column('entry_id', sa.Integer(), nullable=False),
    sa.Column('league_name', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('job_id', sa.String(length=64), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('data_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('analysis_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('manager_key', 'league_id', name=op.f('pk_manager_leagues'))
    )
    op.create_index(op.f('ix_manager_leagues_status'), 'manager_leagues', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_manager_leagues_status'), table_name='manager_leagues')
    op.drop_table('manager_leagues')
