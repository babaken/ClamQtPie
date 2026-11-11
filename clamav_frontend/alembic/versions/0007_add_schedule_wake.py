"""schedule: wake

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-02 16:00:00

サービス化・Web API設計書 6.6節・9.1節の、スリープ中でも起こして実行するジョブ（設計書の案では 0006）。
既存のジョブは起こさない（ノート PC が鞄の中などで起動しないよう、既定は false）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('schedule') as batch:
        batch.add_column(sa.Column('wake', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('schedule') as batch:
        batch.drop_column('wake')
