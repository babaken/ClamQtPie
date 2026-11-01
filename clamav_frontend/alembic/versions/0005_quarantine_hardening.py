"""quarantine: encoding, owner_info, original_path_raw

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01 21:00:00

サービス化・Web API設計書 6.3節の、隔離ファイルの変換（設計書の案では 0008）と所有者の復帰（同 0007）を
1つにまとめた。あわせて、UTF-8 にできない名前のファイルを隔離できるよう、元のパスのバイト列を持つ。
既存の行は encoding が none（変換していない隔離ファイル。変換し直さない）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('quarantine') as batch:
        batch.add_column(sa.Column('encoding', sa.Text(), nullable=False, server_default='none'))
        batch.add_column(sa.Column('owner_info', sa.Text(), nullable=True))
        batch.add_column(sa.Column('original_path_raw', sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('quarantine') as batch:
        batch.drop_column('original_path_raw')
        batch.drop_column('owner_info')
        batch.drop_column('encoding')
