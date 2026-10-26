"""
旧名（ClamAVフロントエンド / ClamQt）で登録した内容を、新しい名前（ClamQtPie）へ引き継ぐ

製品名の変更でOSに登録する識別子を変えた（core/version.py）。旧版から置き換えた環境で、
次のものが旧名のまま残って動かなくなる・二重に登録されるのを防ぐため、起動時に1回ずつ確認する。
- SMTPパスワード（キーリングのサービス名 ClamAVFrontend → ClamQtPie）: 新しいサービス名へ移す
- 自動起動（Runキーの値 ClamAVFrontend / clamav-frontend.desktop）: 旧版の実行ファイルを指しているため、
  消して現在の実行ファイルで登録し直す
- 通知クリック用のURLスキーム（clamqt:）: 旧版の補助プログラムを指しているため消す（新しい clamqtpie: は
  register_protocolで登録する）。本アプリが登録したものと確認できた場合だけ消す
- ユーザー単位のフォルダ（%LOCALAPPDATA%\\clamav-frontend・~/.local/share/clamav-frontend）: 残った依頼ファイル・
  ロックファイルを消し、空になったフォルダを消す

どれも失敗しても起動は止めない（ログに記録するだけ）。
"""
from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

from core.logger import CATEGORY_SYSTEM, log_event
from core.version import LEGACY_APP_SLUG, LEGACY_PROTOCOL

logger = logging.getLogger("clamav_frontend")

#: 旧版の補助プログラム（URLスキームの登録が本アプリのものか確かめるため）
_LEGACY_HELPER_MARKERS = ("ClamAVFrontendActivate.exe", "notify_activate.py")
#: 旧版のユーザー単位のフォルダに置いていたファイル
_LEGACY_STATE_FILES = ("show_request", "show_request.tmp", "app.lock")


def migrate_legacy_names(config_service, autostart_service, winreg_module=None) -> list[str]:
    """旧名の登録を引き継ぐ。引き継いだ項目の説明を返す（ログにも記録する）"""
    migrated: list[str] = []
    if config_service.migrate_legacy_smtp_password():
        migrated.append("SMTPパスワード")
    try:
        if autostart_service.migrate_legacy():
            migrated.append("OS起動時の自動起動")
    except Exception as exc:  # noqa: BLE001 - 起動は止めない
        logger.warning(f"旧名の自動起動の登録を引き継げませんでした (detail: {exc})")
    if remove_legacy_protocol(winreg_module):
        migrated.append(f"通知クリック用のURLスキーム（{LEGACY_PROTOCOL}:）の削除")
    if remove_legacy_state_dir():
        migrated.append(f"旧フォルダ（{LEGACY_APP_SLUG}）の削除")
    if migrated:
        log_event(CATEGORY_SYSTEM, f"旧名で登録した内容を引き継ぎました: {'、'.join(migrated)}")
    return migrated


def remove_legacy_protocol(winreg_module=None) -> bool:
    """旧名のURLスキーム（HKCU\\Software\\Classes\\clamqt）を消す（Windowsのみ）。消したらTrue"""
    if platform.system() != "Windows" and winreg_module is None:
        return False
    try:
        winreg = winreg_module or __import__("winreg")
        base = rf"Software\Classes\{LEGACY_PROTOCOL}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as key:
                command, _ = winreg.QueryValueEx(key, "")
        except FileNotFoundError:
            return False
        if not any(marker in command for marker in _LEGACY_HELPER_MARKERS):
            # 本アプリが登録したものと確認できない（他のアプリが同じ名前を使っている）場合は消さない
            return False
        for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + sub)
        return True
    except OSError as exc:
        logger.warning(f"旧名のURLスキームを削除できませんでした (detail: {exc})")
        return False


def legacy_state_dir() -> Path:
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / LEGACY_APP_SLUG
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / LEGACY_APP_SLUG


def remove_legacy_state_dir(directory: Path | None = None) -> bool:
    """旧名のユーザー単位のフォルダから本アプリのファイルを消し、空になればフォルダも消す。フォルダを消したらTrue"""
    directory = directory or legacy_state_dir()
    if not directory.is_dir():
        return False
    try:
        for name in _LEGACY_STATE_FILES:
            (directory / name).unlink(missing_ok=True)
        directory.rmdir()  # 他のファイルが残っていれば消さない（OSError）
        return True
    except OSError as exc:
        logger.debug(f"旧名のフォルダを削除できませんでした (detail: {exc})")
        return False
