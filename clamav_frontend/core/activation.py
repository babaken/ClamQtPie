"""
デスクトップ通知のクリックによる画面表示（Windows Toast → 隔離画面）

参照: 通知機能詳細仕様書4.1節「アクション: クリックでメインウィンドウの隔離画面を開く」・8章 未確定事項1
Phase 11で実装。仕組み:
  1. 検知のToastに activationType="protocol" launch="clamqtpie:quarantine" を付ける
  2. URLスキーム clamqtpie: をユーザー単位のレジストリ（HKCU\\Software\\Classes\\clamqtpie）に登録する。
     クリックされると、ウィンドウを持たず権限昇格も不要な補助プログラム（notify_activate.py、配布版は
     ClamQtPieActivate.exe）が依頼ファイル（固定内容 "quarantine"）を書く
     （本アプリのexeは常に管理者権限で起動するため、exeを直接起動させるとクリックのたびにUACが出てしまう。
     またPowerShellではコンソールが一瞬表示されてしまう）
  3. 起動中のアプリは依頼ファイルのフォルダをQFileSystemWatcherで監視し、依頼を受けたら隔離画面を開く
     （タスクトレイ常駐アプリプロセス設計書3.2節の「プロセス間通信は実装しない」方針を、ファイル1つの
     受け渡しに限って緩和したもの。QtNetworkは配布物から除外しているためローカルソケットは使わない）
安全性: 外部（ブラウザ等）から clamqtpie: を開かれても、書かれる内容は固定で、許可した画面名以外は無視する。
        URLの内容をコマンドラインに埋め込まない（コマンドインジェクションの防止）。
"""
from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from core.version import APP_ID, APP_SLUG

logger = logging.getLogger("clamav_frontend")

PROTOCOL = APP_SLUG
SCREEN_QUARANTINE = "quarantine"
#: 通知のクリックで開いてよい画面（依頼ファイルの内容はこれ以外を無視する）
ALLOWED_SCREENS = (SCREEN_QUARANTINE,)
REQUEST_FILE_NAME = "show_request"
#: 依頼ファイルが書き込み中で読めない場合の読み直し（200ms間隔で最大10回＝約2秒）
_READ_RETRY_COUNT = 10
_READ_RETRY_INTERVAL_MS = 200


def user_state_dir() -> Path:
    """ユーザー単位のアプリ状態フォルダ（管理者権限のアプリと一般ユーザーのプロセスの両方から書ける場所）"""
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_SLUG
    from core.single_instance import default_lock_dir

    return default_lock_dir()


def toast_launch_uri(screen: str) -> str:
    return f"{PROTOCOL}:{screen}"


#: 通知クリック時に起動する補助プログラム（notify_activate.py をconsole=False・UAC不要でexe化したもの）
HELPER_EXE_NAME = f"{APP_ID}Activate.exe"


def helper_command(app_dir: Path, frozen: bool, python_executable: str) -> list[str]:
    """通知クリック時に起動する補助プログラムのコマンド（ウィンドウを持たず、管理者権限も要求しない）

    以前はPowerShellで依頼ファイルを書いていたが、-WindowStyle Hiddenでもコンソールが一瞬表示されたため変更した。
    - 配布版: 本体と同じフォルダの ClamQtPieActivate.exe
    - 開発時: python.exeと同じ場所の pythonw.exe で notify_activate.py を実行
    """
    if frozen:
        return [str(app_dir / HELPER_EXE_NAME)]
    pythonw = Path(python_executable).with_name("pythonw.exe")
    return [str(pythonw), str(app_dir / "notify_activate.py")]


def protocol_command(helper: list[str]) -> str:
    """URLスキームのクリック時に実行するコマンド行。URLの内容（%1）は渡さない（コマンドインジェクションの防止）"""
    return " ".join(f'"{part}"' for part in helper)


def register_protocol(helper: list[str], winreg_module=None) -> bool:
    """clamqtpie: をユーザー単位で登録する（Windowsのみ）。失敗しても通知のクリックが効かないだけなのでFalseを返す"""
    if platform.system() != "Windows" and winreg_module is None:
        return False
    try:
        winreg = winreg_module or __import__("winreg")
        base = rf"Software\Classes\{PROTOCOL}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"URL:{APP_ID}")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, protocol_command(helper))
        return True
    except OSError as exc:
        logger.warning(f"通知クリック用のURLスキームを登録できませんでした (detail: {exc})")
        return False


class ActivationWatcher(QObject):
    """依頼ファイルを監視し、通知のクリックで開く画面名を知らせる"""

    show_requested = Signal(str)  # 画面キー（"quarantine"）

    def __init__(self, state_dir: Path) -> None:
        super().__init__()
        self._state_dir = state_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        # 起動前に残っていた古い依頼は無視する（アプリ停止中のクリックで勝手に開かない）
        self.request_file.unlink(missing_ok=True)
        self._watcher = QFileSystemWatcher([str(self._state_dir)])
        self._watcher.directoryChanged.connect(lambda _path: self.check_request())

    @property
    def request_file(self) -> Path:
        return self._state_dir / REQUEST_FILE_NAME

    def check_request(self, attempt: int = 0) -> None:
        try:
            content = self.request_file.read_text(encoding="ascii", errors="ignore").strip()
            self.request_file.unlink(missing_ok=True)
        except FileNotFoundError:
            return
        except OSError as exc:
            # 書き込み側（PowerShell）がファイルを開いている間はWindowsでは読めない（Permission denied）。
            # フォルダの変更通知は書き込み完了前に届くため、少し待って読み直す（Windows実機テストで発見）
            if attempt < _READ_RETRY_COUNT:
                QTimer.singleShot(_READ_RETRY_INTERVAL_MS, lambda: self.check_request(attempt + 1))
            else:
                logger.warning(f"通知クリックの依頼を読み取れませんでした (detail: {exc})")
            return
        if content in ALLOWED_SCREENS:
            self.show_requested.emit(content)
