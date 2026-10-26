"""
DatabaseService

参照: DBマイグレーション設計書4章・5章
- app.db は実行ファイル同階層（ポータブル構成）に配置
- 起動時、確認なしでAlembic最新リビジョンまで自動適用する（1章の決定事項）
"""
from __future__ import annotations

import logging
import os
import platform
import stat
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.exceptions import DatabaseConnectionError, DatabaseMigrationError

logger = logging.getLogger("clamav_frontend")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _PROJECT_ROOT / "alembic.ini"
_ALEMBIC_SCRIPT_LOCATION = _PROJECT_ROOT / "alembic"


class DatabaseService:
    """app.dbへの接続とAlembicマイグレーション適用を担う"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._engine = None
        self._session_factory: sessionmaker | None = None

    def connect(self) -> None:
        """DB接続を確立し、必要であれば自動的にマイグレーションを適用する

        参照: DBマイグレーション設計書5.1節の起動時シーケンス
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # app.dbには隔離した元ファイルのパス・ウイルス名・監視対象等が入る。SQLiteが作成すると
        # umask次第で誰でも読める権限（0644等）になり、マルチユーザー環境で他ユーザーに読まれ得る
        # （セキュリティレビューで発見）。所有者のみ読み書き可（0600）にしてから接続する
        self._restrict_db_permissions()
        db_url = f"sqlite:///{self.db_path}"

        try:
            self._engine = create_engine(db_url, future=True)
        except Exception as exc:
            raise DatabaseConnectionError(detail=str(exc)) from exc

        self.migrate()
        self._session_factory = sessionmaker(bind=self._engine, future=True)

    def _restrict_db_permissions(self) -> None:
        """app.dbと付随ファイル（-wal/-shm/-journal）を所有者のみ読み書き可にする（POSIXのみ）。

        Windowsはパーミッションの仕組みが異なり、アプリのフォルダも利用者管理下のため対象外。
        失敗しても起動は止めない（best-effort）。
        """
        if platform.system() == "Windows":
            return
        owner_rw = stat.S_IRUSR | stat.S_IWUSR  # 0o600
        # 世界から読める瞬間を作らないよう、存在しなければ先に0600で作成する
        if not self.db_path.exists():
            try:
                fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, owner_rw)
                os.close(fd)
            except FileExistsError:
                pass
            except OSError as exc:
                logger.warning(f"app.dbの作成時に権限を設定できませんでした (detail: {exc})")
        for path in (self.db_path, *(self.db_path.with_name(self.db_path.name + s) for s in ("-wal", "-shm", "-journal"))):
            try:
                if path.exists():
                    os.chmod(path, owner_rw)
            except OSError as exc:
                logger.warning(f"{path.name}の権限を制限できませんでした (detail: {exc})")

    def migrate(self) -> None:
        """Alembicの最新リビジョンまで自動適用する（確認なし、DBマイグレーション設計書5.2節）"""
        alembic_cfg = Config(str(_ALEMBIC_INI))
        alembic_cfg.set_main_option("script_location", str(_ALEMBIC_SCRIPT_LOCATION))

        try:
            with self._engine.connect() as connection:
                alembic_cfg.attributes["connection"] = connection
                command.upgrade(alembic_cfg, "head")
        except Exception as exc:
            logger.error(
                f"データベースの更新に失敗しました (detail: {exc})",
                extra={"error_code": "E5002"},
            )
            raise DatabaseMigrationError(detail=str(exc)) from exc

    def session(self) -> Session:
        if self._session_factory is None:
            raise DatabaseConnectionError(detail="DatabaseService.connect()が未実行です")
        return self._session_factory()

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
