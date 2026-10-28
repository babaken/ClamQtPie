"""
SingleInstanceLock — 多重起動防止

参照: タスクトレイ常駐アプリプロセス設計書3章
- Windows: named mutex（CreateMutex）
- Linux: ロックファイル（fcntl.flock、LOCK_EX | LOCK_NB）
後発プロセスは多重起動を検知した時点でサイレント終了する（3.2節）。

ミューテックス・ロックファイルは他のプログラムも作れる（同じ名前を先に作っておけば、本アプリを起動させない
ことができてしまう）。そのため、ロックを取れなかったときは、本アプリ（同じ実行ファイル）の別のプロセスが
実際に動いているかを確かめ、動いていなければWARNINGを記録して起動を続ける。
"""
from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path
from typing import Callable

from core.version import APP_ID, APP_SLUG

logger = logging.getLogger("clamav_frontend")

_MUTEX_NAME = f"Global\\{APP_ID}_SingleInstance"


def default_lock_dir() -> Path:
    """Linuxのロックファイル置き場（タスクトレイ常駐アプリプロセス設計書3.1節: ~/.local/share/clamqtpie）

    ユーザー単位の場所にすることで、アプリを別フォルダに2つ展開しても二重に起動しない
    （Windowsのnamed mutexと同じ振る舞い。Phase 11で設計書どおりに修正）。XDG_DATA_HOMEがあれば従う。
    """
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / APP_SLUG


class SingleInstanceLock:
    """`with`文またはacquire()/release()で使用する多重起動防止ロック"""

    def __init__(
        self,
        lock_dir: Path | None = None,
        other_instance_running: Callable[[], bool] | None = None,
    ) -> None:
        """lock_dir: Linuxのロックファイル置き場（省略時はdefault_lock_dir()。Windowsでは使わない）
        other_instance_running: 本アプリの別のプロセスが動いているか（省略時はanother_instance_running。テスト用の差し替え口）
        """
        self._lock_dir = lock_dir or default_lock_dir()
        self._other_instance_running = other_instance_running or another_instance_running
        self._is_windows = platform.system() == "Windows"
        self._mutex_handle = None
        self._lock_file = None
        self._lock_fd = None

    def acquire(self) -> bool:
        """ロック取得を試みる。取得できた場合True（多重起動でない）を返す"""
        if self._is_windows:
            return self._acquire_windows()
        return self._acquire_unix()

    def _acquire_windows(self) -> bool:
        try:
            import pywintypes
            import win32event
            import win32api
            import winerror

            try:
                self._mutex_handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
            except pywintypes.error as exc:
                # 管理者権限で起動中のインスタンスが作成したGlobal mutexには、
                # 通常権限のプロセスからはアクセス拒否になる。これも「起動済み」の候補とする
                # （Windows実機テストで発見。未処理だと起動時に例外で落ちていた）
                if exc.winerror == winerror.ERROR_ACCESS_DENIED:
                    return not self._is_really_running()
                raise
            last_error = win32api.GetLastError()
            if last_error == winerror.ERROR_ALREADY_EXISTS:
                return not self._is_really_running()
            return True
        except ImportError:
            # pywin32が無い開発環境（Linux等）でのフォールバック
            logger.warning("pywin32が利用できないため、多重起動防止をスキップします")
            return True

    def _acquire_unix(self) -> bool:
        import fcntl

        self._lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_dir / "app.lock"
        self._lock_file = open(lock_path, "w")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file.write(str(id(self)))
            self._lock_file.flush()
            return True
        except OSError:
            self._lock_file.close()
            self._lock_file = None
            return not self._is_really_running()

    def _is_really_running(self) -> bool:
        """ロックを取れなかったときに、本アプリの別のプロセスが実際に動いているかを確かめる。
        動いていなければ（他のプログラムが同じ名前のロックを作っている）、WARNINGを記録して起動を続ける"""
        if self._other_instance_running():
            return True
        logger.warning(
            "多重起動防止のロックを取れませんでしたが、本アプリの別のプロセスは動いていないため起動を続けます"
            "（他のプログラムが同じ名前のロックを作っている可能性があります）"
        )
        return False

    def release(self) -> None:
        """正常終了時のロック解放（3.3節）"""
        if self._is_windows:
            if self._mutex_handle is not None:
                try:
                    import win32api

                    win32api.CloseHandle(self._mutex_handle)
                except ImportError:
                    pass
                self._mutex_handle = None
            return

        if self._lock_file is not None:
            import fcntl

            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._lock_file.close()
            self._lock_file = None
            lock_path = self._lock_dir / "app.lock"
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def another_instance_running() -> bool:
    """本アプリと同じ実行ファイルの、別のプロセスが動いているか

    実行ファイルのフルパスで比べる。確かめられないプロセス（アクセスが拒否された等）で名前が同じものは、
    動いているとみなす（本物の起動中のインスタンスを見落として二重に起動しないため）。
    """
    own = os.path.normcase(os.path.realpath(sys.executable))
    try:
        if platform.system() == "Windows":
            return _windows_other_process_with_image(own)
        return _posix_other_process_with_image(own)
    except Exception as exc:  # noqa: BLE001 - 確かめられない場合は従来どおり「起動済み」とみなす
        logger.warning(f"多重起動の確認に失敗したため、起動済みとみなします (detail: {exc})")
        return True


def _windows_other_process_with_image(own: str) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot")
    own_pid = os.getpid()
    own_name = os.path.basename(own)
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID != own_pid and os.path.normcase(entry.szExeFile) == own_name:
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID)
                if not handle:
                    return True  # 確かめられない同じ名前のプロセス（権限の違い等）は動いているとみなす
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(buffer))
                    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        return True
                    if os.path.normcase(os.path.realpath(buffer.value)) == own:
                        return True
                finally:
                    kernel32.CloseHandle(handle)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel32.CloseHandle(snapshot)


def _posix_other_process_with_image(own: str) -> bool:
    own_pid = os.getpid()
    uid = os.getuid()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == own_pid:
            continue
        try:
            if proc.stat().st_uid != uid:
                continue  # ロックファイルは利用者ごとのため、同じ利用者のプロセスだけを見る
            if os.path.normcase(os.path.realpath(proc / "exe")) == own:
                return True
        except OSError:
            continue  # 終了した・読めないプロセス
    return False
