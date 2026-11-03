"""
管理者（admin）の操作を、昇格ヘルパーに頼んで実行する（サービス化・Web API設計書 3.2節・10章。段階 12-4c）

トレイGUI は admin のトークンを持たない。admin の操作（api_client/admin_ops.py）は、操作の一覧を昇格ヘルパー
（elevate_main.py）に渡し、ヘルパーが確認画面を出してから実行する。1 回の昇格で一覧をまとめて実行する。
- Windows: ClamQtPieElevate.exe を UAC で起動する（ShellExecuteEx の runas）。昇格したプロセスには標準入出力を
  つなげないため、名前付きパイプ（このプロセスが作る。名前は毎回の乱数、最初の 1 つだけ、リモートからは不可）で
  受け渡す。つないできたのが起動したヘルパーかをプロセス ID で確かめる
- Linux: pkexec でヘルパーを起動し、標準入出力で受け渡す（polkit は auth_admin）
- 受け渡しは「長さ（4 バイト）＋JSON」（elevate_main.frame）

ElevatedCaller は SyncCaller と同じ形（call・items）で、admin の操作だけを昇格ヘルパーに回す。user の操作が
403（E8102）になったときも、一覧にある操作なら 1 回だけ昇格してやり直す（フルスキャン・自分のフォルダの外のスキャンなど）。
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import logging
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication

from api_client.admin_ops import Operation, is_admin, match
from core.exceptions import (
    ApiConflictError,
    ApiForbiddenError,
    AppError,
    ElevationCancelledError,
    ElevationError,
)

logger = logging.getLogger("clamav_frontend")

#: Windows の配布版の昇格ヘルパー（本体と同じフォルダ）
HELPER_EXE_NAME = "ClamQtPieElevate.exe"
#: Linux の配布版の昇格ヘルパー（本体と同じフォルダ。polkit のポリシーはこのパスに対して書く）
HELPER_LINUX_NAME = "clamqtpie-elevate"
_ERROR_CANCELLED = 1223  # UAC の確認で「いいえ」
_PKEXEC_DISMISSED, _PKEXEC_NOT_AUTHORIZED = 126, 127
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_ERROR_IO_PENDING, _ERROR_PIPE_CONNECTED, _ERROR_BROKEN_PIPE = 997, 535, 109
_APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


@dataclass
class Launched:
    """起動した昇格ヘルパー（Windows）。handle は待てるハンドル（終わったら合図になる）"""

    handle: object
    pid: int


# -- 起動 ----------------------------------------------------------------------------------------
def windows_helper(pipe_name: str) -> tuple[str, str]:
    """(実行ファイル, 引数)。開発時は pythonw.exe で elevate_main.py を動かす"""
    if getattr(sys, "frozen", False):
        return str(_APP_DIR / HELPER_EXE_NAME), f"--pipe {pipe_name}"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw), f'"{_APP_DIR / "elevate_main.py"}" --pipe {pipe_name}'


def launch_windows(pipe_name: str) -> Launched:
    import pywintypes
    import win32con
    import win32process
    from win32com.shell import shell, shellcon

    exe, params = windows_helper(pipe_name)
    try:
        info = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpVerb="runas", lpFile=exe,
                                    lpParameters=params, nShow=win32con.SW_SHOWNORMAL)
    except pywintypes.error as exc:
        if exc.winerror == _ERROR_CANCELLED:
            raise ElevationCancelledError() from exc
        raise ElevationError(detail=f"昇格ヘルパーを起動できません ({exc.strerror})") from exc
    handle = info["hProcess"]
    return Launched(handle, win32process.GetProcessId(handle))


def linux_helper_command() -> list[str]:
    from core.exec_path import find_system_tool

    pkexec = find_system_tool("pkexec")
    if pkexec is None:
        raise ElevationError(detail="pkexec が見つかりません")
    if getattr(sys, "frozen", False):
        # 画面の表示先は polkit のポリシー（org.freedesktop.policykit.exec.allow_gui）で引き継ぐ（段階 12-4d）
        return [pkexec, str(_APP_DIR / HELPER_LINUX_NAME)]
    # 開発時: pkexec は環境変数を消すため、確認画面の表示先（X11・XWayland）を渡す
    display = [f"{name}={os.environ[name]}" for name in ("DISPLAY", "XAUTHORITY") if os.environ.get(name)]
    return [pkexec, "env", *display, "QT_QPA_PLATFORM=xcb", sys.executable, str(_APP_DIR / "elevate_main.py")]


# -- 受け渡し ------------------------------------------------------------------------------------
def exchange_linux(payload: bytes, command: list[str] | None = None) -> bytes:
    from elevate_main import frame, read_frame

    try:
        process = subprocess.run(command or linux_helper_command(), input=frame(payload), capture_output=True,
                                 check=False)
    except OSError as exc:
        raise ElevationError(detail=f"昇格ヘルパーを起動できません ({exc})") from exc
    if not process.stdout:
        if process.returncode in (_PKEXEC_DISMISSED, _PKEXEC_NOT_AUTHORIZED):
            raise ElevationCancelledError()
        raise ElevationError(detail=f"昇格ヘルパーが応答しませんでした（終了コード {process.returncode}）")
    try:
        return read_frame(io.BytesIO(process.stdout).read)
    except ValueError as exc:
        raise ElevationError(detail=str(exc)) from exc


def exchange_windows(payload: bytes, launch=launch_windows) -> bytes:
    import pywintypes
    import win32event
    import win32file
    import win32pipe

    from elevate_main import frame, read_frame

    name = f"clamqtpie-elevate-{secrets.token_hex(16)}"
    pipe = win32pipe.CreateNamedPipe(
        rf"\\.\pipe\{name}",
        win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED | _FILE_FLAG_FIRST_PIPE_INSTANCE,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
        1, 65536, 65536, 0, None)
    try:
        launched = launch(name)

        def wait(event) -> None:
            """event を待つ。先にヘルパーが終わったら（つながない・書かないまま）エラー"""
            win32event.WaitForMultipleObjects([event, launched.handle], False, win32event.INFINITE)
            if win32event.WaitForSingleObject(event, 0) != win32event.WAIT_OBJECT_0:
                import win32process

                code = win32process.GetExitCodeProcess(launched.handle)
                raise ElevationError(detail=f"昇格ヘルパーが終了しました（終了コード {code}）")

        def overlapped():
            ov = pywintypes.OVERLAPPED()
            ov.hEvent = win32event.CreateEvent(None, True, False, None)
            return ov

        ov = overlapped()
        if win32pipe.ConnectNamedPipe(pipe, ov) == _ERROR_IO_PENDING:
            wait(ov.hEvent)
        if win32pipe.GetNamedPipeClientProcessId(pipe) != launched.pid:
            raise ElevationError(detail="起動した昇格ヘルパー以外がつないできました")

        ov = overlapped()
        win32file.WriteFile(pipe, frame(payload), ov)
        wait(ov.hEvent)
        win32file.GetOverlappedResult(pipe, ov, True)

        def read(size: int) -> bytes:
            ov = overlapped()
            buffer = win32file.AllocateReadBuffer(size)
            try:
                win32file.ReadFile(pipe, buffer, ov)
                wait(ov.hEvent)
                count = win32file.GetOverlappedResult(pipe, ov, True)
            except pywintypes.error as exc:
                if exc.winerror == _ERROR_BROKEN_PIPE:
                    return b""
                raise
            return bytes(buffer[:count])

        try:
            return read_frame(read)
        except ValueError as exc:
            raise ElevationError(detail=str(exc)) from exc
    except pywintypes.error as exc:
        raise ElevationError(detail=f"昇格ヘルパーとの受け渡しに失敗しました ({exc.strerror})") from exc
    finally:
        win32file.CloseHandle(pipe)


# -- 実行 ----------------------------------------------------------------------------------------
class ElevationRunner:
    """操作の一覧を昇格ヘルパーで実行する。exchange は (送る内容) → 受け取った内容 の関数"""

    def __init__(self, exchange=None) -> None:
        self._exchange = exchange or (exchange_windows if sys.platform == "win32" else exchange_linux)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="elevate")
        self._busy = False

    def run(self, operations: list[Operation]) -> list:
        """実行して、操作ごとの応答の JSON を返す。取り消したら ElevationCancelledError、
        途中で失敗したらその AppError（completed に実行できた件数）"""
        if self._busy:
            raise ApiConflictError("別の管理者の操作を確認しています。先にそちらを終えてください")
        self._busy = True
        try:
            payload = json.dumps({"operations": [op.to_json() for op in operations]}).encode("utf-8")
            future = self._executor.submit(self._exchange, payload)
            _wait(future)
            raw = future.result()
        finally:
            self._busy = False
        return _results(raw)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def _wait(future: concurrent.futures.Future) -> None:
    """画面のスレッドでは、画面を止めずに待つ（ヘルパーの確認画面で利用者が考えている間）"""
    app = QApplication.instance()
    if app is None or QThread.currentThread() is not app.thread():
        concurrent.futures.wait([future])
        return
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: loop.quit() if future.done() else None)
    timer.start(50)
    while not future.done():
        loop.exec()
    timer.stop()


def _results(raw: bytes) -> list:
    from api_client.client import error_from_body

    try:
        result = json.loads(raw.decode("utf-8"))
        status = result["status"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise ElevationError(detail="昇格ヘルパーの応答が正しくありません") from exc
    if status == "done":
        return list(result.get("results") or [])
    if status == "cancelled":
        raise ElevationCancelledError()
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    exc = error_from_body(500, json.dumps({"error": error}).encode("utf-8")) if error else ElevationError()
    exc.completed = int(result.get("completed") or 0)
    raise exc


class ElevatedCaller:
    """SyncCaller の代わり。admin の操作だけを昇格ヘルパーに回す"""

    def __init__(self, api, runner: ElevationRunner) -> None:
        self._api = api
        self.runner = runner

    @property
    def client(self):
        return self._api.client

    def call(self, method: str, path: str, body=None, params: dict | None = None):
        if params is None and is_admin(method, path):
            return self.runner.run([Operation(method, path, body)])[0]
        try:
            return self._api.call(method, path, body, params)
        except ApiForbiddenError:
            if params is not None or match(method, path) is None:
                raise
            logger.info(f"管理者の権限で実行し直します: {method} {path}")
            return self.runner.run([Operation(method, path, body)])[0]

    def items(self, path: str, params: dict | None = None) -> list[dict]:
        return self._api.items(path, params)

    def batch(self, operations: list[Operation]) -> list:
        """1 回の昇格でまとめて実行する"""
        if not operations:
            return []
        return self.runner.run(operations)


def is_cancelled(error: AppError) -> bool:
    return isinstance(error, ElevationCancelledError)
