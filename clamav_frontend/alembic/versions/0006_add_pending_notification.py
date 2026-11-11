"""add pending_notification

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-02 14:00:00

サービス化・Web API設計書 7.1節の通知の保存（設計書の案では 0004。スキャン履歴・隔離の変更が先に入ったため 0006 にした）。
サービスはデスクトップに通知を出せないため、未読として保存し、トレイGUI・WebUI が読んで表示する。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('pending_notification',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('open_screen', sa.Text(), nullable=True),
    sa.Column('acked_at', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_pending_notification_acked_at', 'pending_notification', ['acked_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('pending_notification')
