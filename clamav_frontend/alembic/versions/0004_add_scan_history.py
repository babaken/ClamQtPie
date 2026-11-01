"""add scan_history

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01 18:00:00

サービス化・Web API設計書 6.2節のスキャン履歴（設計書の案では 0005。通知の保存（12-3）より先に作るため 0004 にした）。
検知したファイル（scan_history_detection）と、検査できなかったファイル（scan_history_error）も持つ。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('scan_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.Text(), nullable=True),
    sa.Column('finished_at', sa.Text(), nullable=True),
    sa.Column('source', sa.Text(), nullable=True),
    sa.Column('scan_type', sa.Text(), nullable=True),
    sa.Column('label', sa.Text(), nullable=True),
    sa.Column('targets', sa.Text(), nullable=True),
    sa.Column('scanned', sa.Integer(), nullable=True),
    sa.Column('detected', sa.Integer(), nullable=True),
    sa.Column('errors', sa.Integer(), nullable=True),
    sa.Column('errors_truncated', sa.Integer(), nullable=True),
    sa.Column('result', sa.Text(), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_scan_history_started_at', 'scan_history', ['started_at'])
    op.create_table('scan_history_detection',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('history_id', sa.Integer(), sa.ForeignKey('scan_history.id', ondelete='CASCADE'), nullable=False),
    sa.Column('path', sa.Text(), nullable=True),
    sa.Column('virus_name', sa.Text(), nullable=True),
    sa.Column('action', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_scan_history_detection_history_id', 'scan_history_detection', ['history_id'])
    op.create_table('scan_history_error',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('history_id', sa.Integer(), sa.ForeignKey('scan_history.id', ondelete='CASCADE'), nullable=False),
    sa.Column('path', sa.Text(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_scan_history_error_history_id', 'scan_history_error', ['history_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('scan_history_error')
    op.drop_table('scan_history_detection')
    op.drop_table('scan_history')
