"""
サービスのイベント（SSE、GET /events）を受けて Qt の Signal に変える（サービス化・Web API設計書 5章・10章。段階 12-4a）

- 受け取った分ずつ読み（readyRead）、1 件ずつ event(name, data) を出す
- 切れたら disconnected(error) を出し、少し待ってからつなぎ直す。Last-Event-ID で、切れていた間のイベントを
  サービスに送り直させる（サービスは直近 1,000 件を持っている）
- つなぐたびに、待ち受けているのがサービスかを確かめる（ApiClient.authorized_request）
"""
from __future__ import annotations

import json
import logging

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from api_client.client import ApiClient
from core.exceptions import AppError, ServiceUnavailableError

logger = logging.getLogger("clamav_frontend")

#: 切れてからつなぎ直すまでの待ち（ミリ秒）
RECONNECT_MS = 3000
#: 1 件のイベントの大きさの上限（これを超えて区切りが来なければ、つなぎ直す）
_MAX_EVENT_BYTES = 1024 * 1024


class EventStream(QObject):
    #: イベントの名前と内容（JSON）
    event = Signal(str, object)
    connected = Signal()
    #: 切れた（AppError。つなぎ直しは自動で行う）
    disconnected = Signal(object)

    def __init__(self, client: ApiClient, reconnect_ms: int = RECONNECT_MS, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._client = client
        self._reconnect_ms = reconnect_ms
        self._reply: QNetworkReply | None = None
        self._buffer = b""
        self._last_id: int | None = None
        self._running = False
        self._was_connected = False
        #: 切れていることを知らせ済みか（つなぎ直すたびに disconnected を出さないように）
        self._down = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._open)

    @property
    def last_event_id(self) -> int | None:
        return self._last_id

    def start(self) -> None:
        self._running = True
        self._open()

    def stop(self) -> None:
        self._running = False
        self._timer.stop()
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _open(self) -> None:
        if not self._running or self._reply is not None:
            return
        try:
            request = self._client.authorized_request("/events")
        except AppError as exc:
            self._lost(exc)
            return
        request.setRawHeader(b"Accept", b"text/event-stream")
        if self._last_id is not None:
            request.setRawHeader(b"Last-Event-ID", str(self._last_id).encode("ascii"))
        self._buffer = b""
        reply = self._client.network.get(request)
        self._reply = reply
        reply.metaDataChanged.connect(lambda: self._headers(reply))
        reply.readyRead.connect(lambda: self._read(reply))
        reply.finished.connect(lambda: self._finished(reply))

    def _headers(self, reply: QNetworkReply) -> None:
        """応答のヘッダーが届いた（イベントがまだ無くても、つながったことを知らせる）"""
        if reply is not self._reply or self._was_connected:
            return
        if reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) != 200:
            return  # エラーの応答は _finished で扱う
        self._was_connected = True
        self._down = False
        self.connected.emit()

    def _read(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:
            return
        self._headers(reply)
        if not self._was_connected:
            return
        self._buffer += bytes(reply.readAll().data())
        while True:
            end = self._buffer.find(b"\n\n")
            if end < 0:
                break
            block, self._buffer = self._buffer[:end], self._buffer[end + 2:]
            self._dispatch(block)
        if len(self._buffer) > _MAX_EVENT_BYTES:
            logger.warning("イベントが大きすぎるため、つなぎ直します")
            reply.abort()

    def _dispatch(self, block: bytes) -> None:
        name, data_lines, event_id = "message", [], None
        for raw in block.decode("utf-8", errors="replace").split("\n"):
            if not raw or raw.startswith(":"):
                continue  # 空の行・コメント（keepalive）
            field, _, value = raw.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data_lines.append(value)
            elif field == "id":
                event_id = value
        if event_id is not None and event_id.isdigit():
            self._last_id = int(event_id)
        if not data_lines:
            return
        try:
            data = json.loads("\n".join(data_lines))
        except ValueError:
            logger.warning(f"イベントの内容を読めません: {name}")
            return
        self.event.emit(name, data)

    def _finished(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:
            return
        self._reply = None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        message = reply.errorString()
        reply.deleteLater()
        if status == 401:
            self._client.prepare_later()  # サービスが起動し直してトークンが変わった
        self._lost(ServiceUnavailableError(detail=f"イベントの受信が切れました（{status or message}）"))

    def _lost(self, error: AppError) -> None:
        if not self._down:
            self._down = True
            self.disconnected.emit(error)
        self._was_connected = False
        if self._running:
            self._timer.start(self._reconnect_ms)
