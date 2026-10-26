"""
クイックスキャン／フルスキャンの対象パス

参照: 画面設計書2章（クイックスキャン「ホーム/デスクトップ等」、フルスキャン）、
      画面設計書 未確定事項1・タスクトレイ常駐アプリプロセス設計書 未確定事項1（クイックスキャン対象）
Phase 11で利用者確認のうえ決定:
  - クイックスキャン: 感染しやすい場所（デスクトップ・ダウンロード・ドキュメント・一時フォルダ、
                      Windowsはスタートアップフォルダも）
  - フルスキャン: システムドライブのみ（Windows: C:\\ 等、Linux: / から特殊領域を除く）
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

from PySide6.QtCore import QStandardPaths

#: Linuxのフルスキャンで除外する擬似ファイルシステム等（実ファイルではなく、検査すると停止・無限読込の恐れ）
LINUX_FULL_SCAN_SKIP_DIRS = ("/proc", "/sys", "/dev", "/run")


def quick_scan_targets() -> list[str]:
    """存在するフォルダのみを重複なしで返す（フォルダの場所変更・OneDrive移動にはQtの標準パスで追従）"""
    locations = [
        QStandardPaths.StandardLocation.DesktopLocation,
        QStandardPaths.StandardLocation.DownloadLocation,
        QStandardPaths.StandardLocation.DocumentsLocation,
        QStandardPaths.StandardLocation.TempLocation,
    ]
    paths = [QStandardPaths.writableLocation(loc) for loc in locations]
    if platform.system() == "Windows" and os.environ.get("APPDATA"):
        paths.append(
            str(Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")
        )
    return _existing_unique(paths)


def full_scan_targets() -> list[str]:
    if platform.system() == "Windows":
        return [os.environ.get("SystemDrive", "C:") + "\\"]
    return ["/"]


def full_scan_skip_dirs() -> tuple[str, ...]:
    return () if platform.system() == "Windows" else LINUX_FULL_SCAN_SKIP_DIRS


def _existing_unique(paths: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if not p or not Path(p).is_dir():
            continue
        key = os.path.normcase(os.path.normpath(p))
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result
