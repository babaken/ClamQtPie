"""
プロキシから API を同期的に呼ぶ（サービス化・Web API設計書 10章。段階 12-4b）

画面はこれまでサービスのメソッドを同期的に呼んでいた（`quarantine_service.list_all()` など）。各画面を書き換えずに
済むよう、プロキシは同じ形の同期的なメソッドを持ち、中で API を呼んで結果を待つ。

- 画面のスレッドから呼ばれたとき: その場で QEventLoop を回して応答を待つ（画面の描画・入力は止まらない）
- 別のスレッド（BackgroundTask など）から呼ばれたとき: 通信は画面のスレッドの QNetworkAccessManager で行う必要が
  あるため、画面のスレッドに頼んで、結果を待つ
ループバックへの要求なので、通常は数ミリ秒で返る。
"""
from __future__ import annotations

import concurrent.futures
from urllib.parse import urlencode

from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer, Signal, Slot

from api_client.client import ApiClient
from core.exceptions import AppError, ServiceUnavailableError

#: 一覧の 1 回の取得の数（API の上限）
PAGE_SIZE = 1000
#: 同期的な呼び出しを待つ長さ（ミリ秒）
SYNC_TIMEOUT_MS = 20_000


class SyncCaller(QObject):
    """画面のスレッドで作る"""

    _requested = Signal(object)

    def __init__(self, client: ApiClient, timeout_ms: int = SYNC_TIMEOUT_MS, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self._timeout_ms = timeout_ms
        self._requested.connect(self._send_for_thread)

    def call(self, method: str, path: str, body=None, params: dict | None = None):
        """API を呼んで応答の JSON を返す。エラーは AppError を送出する"""
        if params:
            path = f"{path}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        if QThread.currentThread() is self.thread():
            return self._call_here(method, path, body)
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._requested.emit((future, method, path, body))
        try:
            return future.result(timeout=self._timeout_ms / 1000 + 5)
        except concurrent.futures.TimeoutError as exc:
            raise ServiceUnavailableError(detail="サービスの応答がありません") from exc

    def items(self, path: str, params: dict | None = None) -> list[dict]:
        """一覧の API（{"items", "total"}）をすべて読む"""
        result: list[dict] = []
        while True:
            page = self.call("GET", path, params={**(params or {}), "limit": PAGE_SIZE, "offset": len(result)})
            batch = page.get("items", []) if isinstance(page, dict) else []
            result.extend(batch)
            if not batch or len(result) >= page.get("total", 0):
                return result

    def _call_here(self, method: str, path: str, body):
        loop = QEventLoop()
        outcome: dict = {}

        def done(data, error):
            outcome["data"], outcome["error"] = data, error
            loop.quit()

        reply = self.client.request(method, path, body, timeout_ms=self._timeout_ms)
        reply.finished.connect(done)
        if "error" not in outcome:
            # 念のため（通常は転送のタイムアウトで終わる）。待ち終わったら止める（singleShot だと、呼ぶたびに
            # 待ちの間ずっとタイマーとループが残る）
            guard = QTimer()
            guard.setSingleShot(True)
            guard.timeout.connect(lambda: loop.quit())
            guard.start(self._timeout_ms + 1000)
            loop.exec()
            guard.stop()
        if "error" not in outcome:
            raise ServiceUnavailableError(detail="サービスの応答がありません")
        if outcome["error"] is not None:
            raise outcome["error"]
        return outcome["data"]

    @Slot(object)
    def _send_for_thread(self, request) -> None:
        future, method, path, body = request
        if not future.set_running_or_notify_cancel():
            return

        def done(data, error):
            if error is not None:
                future.set_exception(error if isinstance(error, AppError) else ServiceUnavailableError(detail=str(error)))
            else:
                future.set_result(data)

        self.client.request(method, path, body, timeout_ms=self._timeout_ms).finished.connect(done)
