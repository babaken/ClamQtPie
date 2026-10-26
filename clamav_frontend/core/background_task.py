"""
BackgroundTask — 時間のかかる処理を別スレッドで1件ずつ実行し、完了をシグナルで知らせる

clamdの起動・停止・定義の再読み込み・PING（core/clamd_process.py、エンジン サービス画面）や
システム診断（core/diagnostics_service.py）は、sc / systemctl（pkexecの認証待ちを含む）や
clamdへの接続で数秒〜数十秒かかることがあるため、GUIスレッドから直接呼ばない。

実行中かどうかはスレッドの生死（is_alive）ではなくフラグで管理し、finishedを発行する前に下ろす。
finishedはGUIスレッドへキュー経由で届くため、is_aliveで判定すると、受け取った側から見て
まだ実行中に見えることがあった（ボタンが無効のまま戻らない不具合。レビューで指摘）。
"""
from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QObject, Signal


class BackgroundTask(QObject):
    """finished(操作名, 戻り値（失敗時はNone）, 失敗時の例外（成功時はNone）)"""

    finished = Signal(str, object, object)

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def run(self, name: str, func: Callable[[], object]) -> bool:
        """funcを別スレッドで実行する。実行中なら何もせずFalse"""
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._run, args=(name, func), daemon=True).start()
        return True

    def _run(self, name: str, func: Callable[[], object]) -> None:
        result, error = None, None
        try:
            result = func()
        except Exception as exc:  # noqa: BLE001 - 呼び出し元（画面）でエラー表示に変換する
            error = exc
        with self._lock:
            self._running = False
        self.finished.emit(name, result, error)
