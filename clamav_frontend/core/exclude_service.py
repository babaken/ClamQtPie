"""
ExcludeService — 除外リスト（exclude テーブル）のCRUDと照合ロジック

参照: 画面設計書3章（除外設定タブ: ファイル/フォルダ/拡張子/プロセス）、
      Windowsリアルタイム監視詳細設計書4.2節
Phase4では「ファイル/フォルダ/拡張子」の照合をスキャン機能から利用する。
「プロセス」区分はリアルタイム監視（Phase5）でのみ意味を持つため、
is_excluded()の判定対象には含めない。
"""
from __future__ import annotations

import os
from pathlib import PurePath

from db.database import DatabaseService
from db.models import Exclude

VALID_TYPES = ("file", "folder", "extension", "process")


class ExcludeService:
    def __init__(self, db_service: DatabaseService) -> None:
        self.db_service = db_service

    def add(self, type_: str, value: str, enabled: bool = True) -> Exclude:
        if type_ not in VALID_TYPES:
            raise ValueError(f"不正な除外種別です: {type_}")
        with self.db_service.session() as session:
            row = Exclude(type=type_, value=value, enabled=1 if enabled else 0)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def remove(self, exclude_id: int) -> None:
        with self.db_service.session() as session:
            row = session.get(Exclude, exclude_id)
            if row is not None:
                session.delete(row)
                session.commit()

    def set_enabled(self, exclude_id: int, enabled: bool) -> None:
        with self.db_service.session() as session:
            row = session.get(Exclude, exclude_id)
            if row is not None:
                row.enabled = 1 if enabled else 0
                session.commit()

    def list_all(self) -> list[Exclude]:
        with self.db_service.session() as session:
            return list(session.query(Exclude).all())

    def is_excluded(self, path: str) -> bool:
        """ファイル/フォルダ/拡張子の除外ルール（有効なもののみ）に一致するか判定する

        参照: Windowsリアルタイム監視詳細設計書4.2節
        「プロセス」除外はここでは判定しない（同節の既知の制限に準拠）
        """
        return self.make_matcher()(path)

    def make_matcher(self):
        """現時点の除外ルールを1回だけ読み込んだ判定関数を返す（大量ファイルの一括判定用）"""
        rules = [r for r in self.list_all() if r.enabled and r.type in ("file", "folder", "extension")]
        return lambda path: _matches_any(rules, path)


def is_under_any_folder(path: str | os.PathLike, folders) -> bool:
    """pathがfoldersのいずれかと同じか、その配下か（アプリ自身のフォルダの判定用）

    シンボリックリンクを解決してから比較する（監視対象をリンク経由で指定した場合も判定できるように）。
    """
    target = PurePath(_normalize_path(os.path.realpath(path)))
    return any(_is_under_folder(target, os.path.realpath(folder)) for folder in folders)


def _matches_any(rules, path: str) -> bool:
    target = PurePath(_normalize_path(path))
    for rule in rules:
        if rule.type == "file" and str(target) == _normalize_path(rule.value):
            return True
        if rule.type == "folder" and _is_under_folder(target, rule.value):
            return True
        if rule.type == "extension" and target.suffix.lower() == _normalize_ext(rule.value):
            return True
    return False


def _normalize_path(path: str) -> str:
    """比較用にパスを正規化する

    Windowsではパスの大文字小文字を区別せず、"/"と"\\"も同一視されるため、
    normcaseで揃えないと「C:\\Users\\a.txt」と「c:/users/a.txt」が別物と判定される
    （Windows実機テストで発見）。Linuxではnormpathによる冗長な区切りの除去のみ行われる。
    """
    return os.path.normcase(os.path.normpath(path))


def _is_under_folder(path: PurePath, folder: str) -> bool:
    try:
        path.relative_to(PurePath(_normalize_path(folder)))
        return True
    except ValueError:
        return False


def _normalize_ext(ext: str) -> str:
    ext = ext.lower()
    return ext if ext.startswith(".") else f".{ext}"
