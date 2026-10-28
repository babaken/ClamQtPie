"""
clamdサービスの起動制御

参照: 詳細設計書ClamAVService（start_clamd/restart_clamd）・4章「clamd未起動: GUIへ通知・再起動試行」、
      clamdソケット通信プロトコル設計書4章（自動再起動試行は1回のみ）・8章未確定事項2、
      エラーコード一覧 E1006 / E8003
Phase 11で利用者確認のうえ決定: clamdはOSのサービスとして起動する（アプリからclamdプロセスを直接起動しない）
  - Windows: サービス「clamd」（clamd.exe --install-service で登録される既定名）を sc start で起動
             （本アプリは管理者権限で動作するため昇格不要）
  - Linux: systemd の clamav-daemon を systemctl start で起動（一般ユーザー時は pkexec で認証）
画面レイアウト刷新: ClamAV エンジン サービス画面からの停止（sc stop / systemctl stop）を追加
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from typing import Callable

from PySide6.QtCore import QObject, Signal

from core.background_task import BackgroundTask
from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig
from core.exceptions import AppError, ClamdConnectionError, ClamdProcessError
from core.exec_path import system_tool

_DEFAULT_SERVICE_NAMES = {"Windows": "clamd", "Linux": "clamav-daemon"}
#: sc start の「既に開始されています」（ERROR_SERVICE_ALREADY_RUNNING）
_WIN_ALREADY_RUNNING = "1056"
#: sc stop の「サービスは開始されていません」（ERROR_SERVICE_NOT_ACTIVE）
_WIN_NOT_ACTIVE = "1062"


#: OSのサービス名として受け付ける形（先頭は英数字等。「-」で始まるとコマンドのオプションとして解釈され得る）
_SERVICE_NAME_RE = re.compile(r"[A-Za-z0-9_.@][A-Za-z0-9_.@-]{0,255}")


def is_valid_service_name(name: str) -> bool:
    return bool(_SERVICE_NAME_RE.fullmatch(name))


def no_window_kwargs(system: str) -> dict:
    """Windowsでsc等を実行するときにコンソール画面を一瞬表示させない（配布版はコンソールを持たないため、
    子プロセスごとに新しいコンソールが開いてしまう）"""
    if system == "Windows":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def console_text_kwargs(system: str) -> dict:
    """sc / systemctl の出力を文字列として読むときの指定

    Windowsのscは日本語環境ではOEMコードページ（cp932）で出力する。Python 3.15はUTF-8モードが既定のため、
    text=Trueだけではデコードに失敗して出力が空になり、1056（既に起動中）・1062（既に停止中）を判別できなかった
    （Windows実機の検証で発見）。OEMコードページで読み、読めない文字は置き換えて番号の判定を続ける。
    """
    if system == "Windows":
        return {"text": True, "encoding": "oem", "errors": "replace"}
    return {"text": True, "errors": "replace"}


class ClamdServiceController:
    def __init__(
        self,
        config: ClamdConfig,
        system: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.config = config
        self._system = system or platform.system()
        self._run = runner
        self._which = which

    @property
    def service_name(self) -> str:
        return self.config.service_name or _DEFAULT_SERVICE_NAMES.get(self._system, "clamav-daemon")

    def start(self) -> bool:
        """clamdサービスを起動する。失敗時はClamdProcessError（E8003）"""
        return self._control("start", _WIN_ALREADY_RUNNING)

    def stop(self) -> bool:
        """clamdサービスを停止する（ClamAV エンジン サービス画面）。失敗時はClamdProcessError（E8003）

        注意: Debian/Ubuntuでclamav-daemon.socketが有効な場合、停止後もソケットへの接続でsystemdが
        clamdを再び起動することがある（OSの設定のため、アプリ側では制御しない）。
        """
        return self._control("stop", _WIN_NOT_ACTIVE)

    def _control(self, action: str, win_ok_code: str) -> bool:
        if not is_valid_service_name(self.service_name):
            # sc / systemctl の引数として渡すため、オプションとして解釈され得る名前等は使わない
            raise ClamdProcessError(detail=f"サービス名に使えない文字が含まれています: {self.service_name!r}")
        # OSのコマンドは絶対パスで起動する（core/exec_path.py。相対名だとexeのフォルダ等からも探されるため）
        if self._system == "Windows":
            command = [system_tool("sc", self._system), action, self.service_name]
        else:
            command = [system_tool("systemctl", self._system), action, self.service_name]
            if not (hasattr(os, "geteuid") and os.geteuid() == 0):
                command = [self._which("pkexec") or "pkexec", *command]
        try:
            completed = self._run(
                command,
                capture_output=True,
                timeout=60,
                **console_text_kwargs(self._system),
                **no_window_kwargs(self._system),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClamdProcessError(detail=f"{' '.join(command)}: {exc}") from exc

        output = f"{completed.stdout or ''}{completed.stderr or ''}".strip()
        if completed.returncode == 0 or (self._system == "Windows" and win_ok_code in output):
            return True
        raise ClamdProcessError(detail=f"{' '.join(command)} (exit {completed.returncode}): {output}")


class ClamdStarter(QObject):
    """clamdの起動試行（PING→サービス起動→5秒後にPING）をバックグラウンドで行う

    ClamAVService.ensure_available()は最大で数秒〜十数秒ブロックするため、GUIスレッドから直接呼ばない。
    finished(成功したか, 失敗時のAppError)。スレッドの管理はBackgroundTask（core/background_task.py）に任せる
    """

    finished = Signal(bool, object)

    def __init__(self, clamav_service: ClamAVService) -> None:
        super().__init__()
        self._clamav_service = clamav_service
        self._task = BackgroundTask()
        self._task.finished.connect(self._on_task_finished)

    @property
    def is_running(self) -> bool:
        return self._task.is_running

    def start(self) -> bool:
        """起動試行を始める。既に試行中ならFalse（完了のfinishedは先の試行の分だけ届く）"""
        return self._task.run("start", self._clamav_service.ensure_available)

    def _on_task_finished(self, _name: str, ok, error) -> None:
        if error is not None:
            if not isinstance(error, AppError):
                error = ClamdProcessError(detail=str(error))
            self.finished.emit(False, error)
        elif ok:
            self.finished.emit(True, None)
        else:
            self.finished.emit(False, ClamdConnectionError("ClamAVサービスを起動できませんでした", code="E1006"))
