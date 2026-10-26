"""
SingleInstanceLock — 多重起動防止

参照: タスクトレイ常駐アプリプロセス設計書3章
- Windows: named mutex（CreateMutex）
- Linux: ロックファイル（fcntl.flock、LOCK_EX | LOCK_NB）
後発プロセスは多重起動を検知した時点でサイレント終了する（3.2節）。
"""
from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

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

    def __init__(self, lock_dir: Path | None = None) -> None:
        """lock_dir: Linuxのロックファイル置き場（省略時はdefault_lock_dir()。Windowsでは使わない）"""
        self._lock_dir = lock_dir or default_lock_dir()
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
                # 通常権限のプロセスからはアクセス拒否になる。これも「起動済み」とみなす
                # （Windows実機テストで発見。未処理だと起動時に例外で落ちていた）
                if exc.winerror == winerror.ERROR_ACCESS_DENIED:
                    return False
                raise
            last_error = win32api.GetLastError()
            if last_error == winerror.ERROR_ALREADY_EXISTS:
                return False
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
