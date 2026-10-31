"""
QuarantineService — ファイルの隔離・復元・完全削除

参照: 要件定義書3章、画面設計書5章、エラーコード一覧・例外設計書4.3節
- 隔離: UUID名にリネーム、読み取り専用・実行不可権限、DBへメタデータ登録
- DB登録失敗時はファイル操作もロールバックする（E3004）
- 隔離・復元のファイル移動はリンク（ジャンクション・シンボリックリンク）をたどらずに行う（core/safe_fs.py）
"""
from __future__ import annotations

import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from db.database import DatabaseService
from db.models import Quarantine
from core.exclude_service import is_under_any_folder
from core.logger import CATEGORY_QUARANTINE, log_event
from core.exceptions import (
    QuarantineNotFoundError,
    QuarantineRestoreError,
    QuarantineWriteError,
)
from core.safe_fs import quarantine_move, restore_move

_READONLY_NO_EXEC = stat.S_IRUSR  # 読み取り専用・実行不可（オーナーのみ読み取り可）


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuarantineService:
    def __init__(self, db_service: DatabaseService, quarantine_dir: Path) -> None:
        self.db_service = db_service
        self.quarantine_dir = quarantine_dir
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)

    # -- 隔離 -----------------------------------------------------
    def quarantine(self, original_path: str | Path, virus_name: str) -> Quarantine:
        """検知ファイルを隔離する（要件定義書3章、画面設計書5章）"""
        original_path = Path(original_path)
        if is_under_any_folder(original_path, [self.quarantine_dir]):
            # 隔離済みのファイルを隔離し直すと、隔離の記録とファイルの対応が崩れる。リアルタイム監視では
            # 隔離フォルダへの移動が再び検知され、隔離が無限に繰り返された（Linux実機で発見）
            raise QuarantineWriteError(detail=f"隔離フォルダ内のファイルは隔離しません: {original_path}")
        if not original_path.exists():
            raise QuarantineWriteError(detail=f"隔離対象が存在しません: {original_path}")

        file_uuid = str(uuid.uuid4())
        dest_path = self.quarantine_dir / file_uuid

        try:
            # 開いたファイル自体を移し、移した内容のSHA-256と大きさを得る（確認から移動までの間の置き換え対策）
            sha256, size = quarantine_move(original_path, dest_path)
        except OSError as exc:
            raise QuarantineWriteError(detail=str(exc)) from exc

        try:
            os.chmod(dest_path, _READONLY_NO_EXEC)
        except OSError:
            pass  # Windows等、chmodの意味が薄い環境では黙って続行

        try:
            with self.db_service.session() as session:
                row = Quarantine(
                    uuid=file_uuid,
                    original_path=str(original_path),
                    virus_name=virus_name,
                    sha256=sha256,
                    size=size,
                    detected_at=_now_iso(),
                    restored=0,
                    deleted=0,
                )
                session.add(row)
                session.commit()
                session.refresh(row)
        except Exception as exc:
            # E3004: DB登録失敗時はファイル操作もロールバックする
            self._rollback_file_move(dest_path, original_path)
            raise QuarantineWriteError(
                "隔離情報の記録に失敗しました", code="E3004", detail=str(exc)
            ) from exc

        log_event(CATEGORY_QUARANTINE, f"ファイルを隔離しました: {original_path} ({virus_name})")
        return row

    def _rollback_file_move(self, dest_path: Path, original_path: Path) -> None:
        try:
            if dest_path.exists():
                os.chmod(dest_path, stat.S_IRUSR | stat.S_IWUSR)
                restore_move(dest_path, original_path, overwrite=False)
        except OSError:
            pass  # ロールバック自体の失敗はログ側の責務（呼び出し元でログ記録想定）

    # -- 復元 -----------------------------------------------------
    def restore(
        self, quarantine_id: int, dest_path: str | Path | None = None, overwrite: bool = False
    ) -> None:
        """隔離ファイルを復元する（既定は元のパス）

        復元先に同名ファイルがある場合、overwrite=Falseなら上書きせずにE3002とする
        （画面設計書5章: 上書き確認 or 別名指定は画面側で行う）。
        """
        with self.db_service.session() as session:
            row = session.get(Quarantine, quarantine_id)
            if row is None:
                raise QuarantineNotFoundError()

            target = Path(dest_path) if dest_path else Path(row.original_path)
            source = self.quarantine_dir / row.uuid

            if not source.exists():
                raise QuarantineRestoreError(detail=f"隔離ファイルが見つかりません: {source}")
            if os.path.lexists(target) and not overwrite:
                raise QuarantineRestoreError(
                    "復元先に同じ名前のファイルがあります", detail=str(target)
                )

            try:
                os.chmod(source, stat.S_IRUSR | stat.S_IWUSR)
                # 復元先のフォルダをリンクをたどらずに開いて置く。既にあるものがリンク・フォルダ等なら置き換えない
                restore_move(source, target, overwrite)
            except FileExistsError as exc:
                raise QuarantineRestoreError(
                    "復元先に同じ名前のファイルがあります", detail=str(target)
                ) from exc
            except OSError as exc:
                raise QuarantineRestoreError(detail=str(exc)) from exc

            row.restored = 1
            session.commit()
            log_event(CATEGORY_QUARANTINE, f"隔離ファイルを復元しました: {target} ({row.virus_name})")

    # -- 完全削除 ---------------------------------------------------
    def delete_permanently(self, quarantine_id: int) -> None:
        with self.db_service.session() as session:
            row = session.get(Quarantine, quarantine_id)
            if row is None:
                raise QuarantineNotFoundError()

            source = self.quarantine_dir / row.uuid
            try:
                if source.exists():
                    # 隔離時に読み取り専用にしているため、Windowsでは書き込み可に戻さないと
                    # unlink()がPermissionErrorになる（Windows実機テストで発見）
                    os.chmod(source, stat.S_IRUSR | stat.S_IWUSR)
                    source.unlink()
            except OSError as exc:
                raise QuarantineWriteError(detail=str(exc)) from exc

            row.deleted = 1
            session.commit()
            log_event(
                CATEGORY_QUARANTINE,
                f"隔離ファイルを完全に削除しました: {row.original_path} ({row.virus_name})",
            )

    # -- 一覧 -----------------------------------------------------
    def list_all(self) -> list[Quarantine]:
        with self.db_service.session() as session:
            return list(session.query(Quarantine).order_by(Quarantine.detected_at.desc()).all())

    def get(self, quarantine_id: int) -> Quarantine:
        with self.db_service.session() as session:
            row = session.get(Quarantine, quarantine_id)
            if row is None:
                raise QuarantineNotFoundError()
            return row
