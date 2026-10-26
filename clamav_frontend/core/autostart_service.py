"""
AutostartService — OS起動時自動起動の登録/解除

参照: タスクトレイ常駐アプリプロセス設計書6章
- Windows: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run（6.1節）
- Linux: ~/.config/autostart/clamqtpie.desktop（XDG Autostart仕様、6.2節）
製品名の変更で登録名を変えた。旧名（ClamAVFrontend / clamav-frontend.desktop）の登録は
起動時にmigrate_legacy()で新しい名前へ登録し直す（core/legacy_migration.py）。
"""
from __future__ import annotations

import logging
import platform
from pathlib import Path

from core.exceptions import ProcessError
from core.version import APP_ID, APP_NAME, APP_SLUG, LEGACY_APP_ID, LEGACY_APP_SLUG

logger = logging.getLogger("clamav_frontend")

_REGISTRY_VALUE_NAME = APP_ID
_LINUX_DESKTOP_FILENAME = f"{APP_SLUG}.desktop"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class AutostartService:
    def __init__(self, exe_path: str) -> None:
        self.exe_path = exe_path
        self._is_windows = platform.system() == "Windows"

    def enable(self) -> None:
        try:
            if self._is_windows:
                self._enable_windows()
            else:
                self._enable_linux()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"自動起動の設定に失敗しました (detail: {exc})", extra={"error_code": "E8002"}
            )
            raise ProcessError(
                "自動起動の設定に失敗しました", code="E8002", detail=str(exc)
            ) from exc

    def disable(self) -> None:
        try:
            if self._is_windows:
                self._disable_windows()
            else:
                self._disable_linux()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"自動起動の解除に失敗しました (detail: {exc})", extra={"error_code": "E8002"}
            )
            raise ProcessError(
                "自動起動の解除に失敗しました", code="E8002", detail=str(exc)
            ) from exc

    def is_enabled(self) -> bool:
        if self._is_windows:
            return self._is_enabled_windows()
        return self._is_enabled_linux()

    def migrate_legacy(self) -> bool:
        """旧名で登録された自動起動を消し、新しい名前・現在の実行ファイルで登録し直す。引き継いだらTrue

        旧名の登録は旧版の実行ファイル（ClamAVFrontend.exe）のパスを指しているため、そのままでは起動しない。
        """
        if self._is_windows:
            found = self._delete_windows_value(LEGACY_APP_ID)
        else:
            legacy = self._autostart_dir() / f"{LEGACY_APP_SLUG}.desktop"
            found = legacy.exists()
            if found:
                legacy.unlink()
        if found:
            self.enable()
        return found

    # -- Windows（レジストリRunキー、6.1節） -----------------------------------
    def _enable_windows(self) -> None:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            winreg.SetValueEx(key, _REGISTRY_VALUE_NAME, 0, winreg.REG_SZ, self.exe_path)
        finally:
            winreg.CloseKey(key)

    def _disable_windows(self) -> None:
        self._delete_windows_value(_REGISTRY_VALUE_NAME)

    def _delete_windows_value(self, name: str) -> bool:
        """Runキーの値を消す。消した（あった）らTrue"""
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            try:
                winreg.DeleteValue(key, name)
                return True
            except FileNotFoundError:
                return False
        finally:
            winreg.CloseKey(key)

    def _is_enabled_windows(self) -> bool:
        import winreg

        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_QUERY_VALUE,
            )
            try:
                winreg.QueryValueEx(key, _REGISTRY_VALUE_NAME)
                return True
            except FileNotFoundError:
                return False
            finally:
                winreg.CloseKey(key)
        except FileNotFoundError:
            return False

    # -- Linux（XDG Autostart、6.2節） -----------------------------------------
    def _autostart_dir(self) -> Path:
        return Path.home() / ".config" / "autostart"

    def _desktop_file_path(self) -> Path:
        return self._autostart_dir() / _LINUX_DESKTOP_FILENAME

    def _enable_linux(self) -> None:
        autostart_dir = self._autostart_dir()
        autostart_dir.mkdir(parents=True, exist_ok=True)
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            f"Exec={self.exe_path}\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        self._desktop_file_path().write_text(content, encoding="utf-8")

    def _disable_linux(self) -> None:
        path = self._desktop_file_path()
        if path.exists():
            path.unlink()

    def _is_enabled_linux(self) -> bool:
        return self._desktop_file_path().exists()
