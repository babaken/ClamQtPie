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
from core import file_owner, quarantine_codec
from core.exceptions import (
    QuarantineCorruptedError,
    QuarantineNotFoundError,
    QuarantineRestoreConflictError,
    QuarantineRestoreError,
    QuarantineWriteError,
)
from core.safe_fs import quarantine_move, restore_move
from core.text_safety import visible_controls

_READONLY_NO_EXEC = stat.S_IRUSR  # 読み取り専用・実行不可（オーナーのみ読み取り可）


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remove(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # Windows では読み取り専用だと消せない
        os.remove(path)
    except FileNotFoundError:
        pass


def _storable_path(path: str) -> tuple[str, bytes | None]:
    """DB に書く (表示用のパス, 元のバイト列)。UTF-8 にできない名前（Linux の surrogateescape の文字、
    Windows の対になっていないサロゲート）は DB の文字列に書けないため、元のバイト列を別に持つ"""
    try:
        path.encode("utf-8")
        return path, None
    except UnicodeEncodeError:
        raw = os.fsencode(path) if os.name == "posix" else path.encode("utf-8", errors="surrogatepass")
        return visible_controls(path), raw


def _original_path(row: Quarantine) -> str:
    """隔離の記録から、元のパスを（バイト列があればそこから）戻す"""
    if row.original_path_raw:
        raw = bytes(row.original_path_raw)
        return os.fsdecode(raw) if os.name == "posix" else raw.decode("utf-8", errors="surrogatepass")
    return row.original_path


class QuarantineService:
    def __init__(self, db_service: DatabaseService, quarantine_dir: Path) -> None:
        self.db_service = db_service
        self.quarantine_dir = quarantine_dir
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)

    # -- 隔離 -----------------------------------------------------
    def quarantine(self, original_path: str | Path, virus_name: str) -> Quarantine:
        """検知ファイルを隔離する（要件定義書3章、画面設計書5章）

        段階 12-2d: 隔離したファイルは変換して保存し（core/quarantine_codec.py）、元の所有者・権限を記録する
        （core/file_owner.py）。UTF-8 にできない名前は、元のバイト列を original_path_raw に持つ
        """
        original_path = Path(original_path)
        if is_under_any_folder(original_path, [self.quarantine_dir]):
            # 隔離済みのファイルを隔離し直すと、隔離の記録とファイルの対応が崩れる。リアルタイム監視では
            # 隔離フォルダへの移動が再び検知され、隔離が無限に繰り返された（Linux実機で発見）
            raise QuarantineWriteError(detail=f"隔離フォルダ内のファイルは隔離しません: {original_path}")
        if not original_path.exists():
            raise QuarantineWriteError(detail=f"隔離対象が存在しません: {original_path}")
        display_path, raw_path = _storable_path(str(original_path))
        owner_info = file_owner.capture(original_path)

        file_uuid = str(uuid.uuid4())
        dest_path = self.quarantine_dir / file_uuid
        moving = self.quarantine_dir / f"{file_uuid}.moving"

        try:
            # 開いたファイル自体を移し、移した内容のSHA-256と大きさを得る（確認から移動までの間の置き換え対策）
            sha256, size = quarantine_move(original_path, moving)
        except OSError as exc:
            raise QuarantineWriteError(detail=str(exc)) from exc

        try:
            # 元の内容のまま隔離フォルダに置かないよう、変換してから元の内容のファイルを消す
            quarantine_codec.encode_file(moving, dest_path, sha256, size)
            _remove(moving)
        except OSError as exc:
            _remove(dest_path)
            self._rollback_file_move(moving, original_path)
            raise QuarantineWriteError(detail=str(exc)) from exc

        try:
            os.chmod(dest_path, _READONLY_NO_EXEC)
        except OSError:
            pass  # Windows等、chmodの意味が薄い環境では黙って続行

        try:
            with self.db_service.session() as session:
                row = Quarantine(
                    uuid=file_uuid,
                    original_path=display_path,
                    original_path_raw=raw_path,
                    virus_name=virus_name,
                    sha256=sha256,
                    size=size,
                    detected_at=_now_iso(),
                    restored=0,
                    deleted=0,
                    encoding=quarantine_codec.ENCODING_XOR_V1,
                    owner_info=owner_info,
                )
                session.add(row)
                session.commit()
                session.refresh(row)
        except Exception as exc:
            # E3004: DB登録失敗時はファイル操作もロールバックする
            self._rollback_encoded(dest_path, original_path)
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

    def _rollback_encoded(self, encoded: Path, original_path: Path) -> None:
        """変換した隔離ファイルを元に戻して、元の場所へ置き直す（DB への記録に失敗したとき）"""
        plain = encoded.with_name(encoded.name + ".restoring")
        try:
            quarantine_codec.decode_file(encoded, plain)
            self._rollback_file_move(plain, original_path)
            _remove(encoded)
        except (OSError, quarantine_codec.QuarantineFormatError):
            pass

    # -- 復元 -----------------------------------------------------
    def restore(
        self, quarantine_id: int, dest_path: str | Path | None = None, overwrite: bool = False
    ) -> None:
        """隔離ファイルを復元する（既定は元のパス）

        復元先に同名ファイルがある場合、overwrite=Falseなら上書きせずにE3002とする
        （画面設計書5章: 上書き確認 or 別名指定は画面側で行う）。
        変換した隔離ファイルは元に戻し、SHA-256 が記録と一致しなければ E3005（復元しない）。
        元の所有者・権限の記録があれば戻す。
        """
        with self.db_service.session() as session:
            row = session.get(Quarantine, quarantine_id)
            if row is None:
                raise QuarantineNotFoundError()

            target = Path(dest_path) if dest_path else Path(_original_path(row))
            source = self.quarantine_dir / row.uuid

            if not source.exists():
                raise QuarantineRestoreError(detail=f"隔離ファイルが見つかりません: {source}")
            if os.path.lexists(target) and not overwrite:
                raise QuarantineRestoreConflictError(detail=visible_controls(str(target)))

            encoded = (row.encoding or quarantine_codec.ENCODING_NONE) == quarantine_codec.ENCODING_XOR_V1
            plain = source.with_name(source.name + ".restoring") if encoded else source
            try:
                if encoded:
                    quarantine_codec.decode_file(source, plain)
                    if row.sha256 and quarantine_codec.read_header(source)[1] != row.sha256:
                        raise quarantine_codec.QuarantineFormatError("SHA-256 が記録と一致しません")
                else:
                    os.chmod(source, stat.S_IRUSR | stat.S_IWUSR)
                # 復元先のフォルダをリンクをたどらずに開いて置く。既にあるものがリンク・フォルダ等なら置き換えない
                restore_move(plain, target, overwrite)
            except quarantine_codec.QuarantineFormatError as exc:
                _remove(plain) if encoded else None
                raise QuarantineCorruptedError(detail=str(exc)) from exc
            except FileExistsError as exc:
                _remove(plain) if encoded else None
                raise QuarantineRestoreConflictError(detail=visible_controls(str(target))) from exc
            except OSError as exc:
                _remove(plain) if encoded else None
                raise QuarantineRestoreError(detail=str(exc)) from exc

            file_owner.apply(target, row.owner_info)
            if encoded:
                _remove(source)
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
