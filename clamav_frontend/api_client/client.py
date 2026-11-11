"""
トレイGUI からサービスの内部 API を呼ぶクライアント（サービス化・Web API設計書 3.1節・3.2節・10章。段階 12-4a）

- 接続先: サービスが書いた api/endpoint.json のポート（無ければ設定の既定のポート）
- トークン: api/user.token（ClamQtPie Users・clamqtpie グループの利用者だけが読める）。読めなければ
  ServicePermissionError（トレイGUI は「この PC の ClamQtPie を使う権限がありません」と案内して終わる）
- トークンを送る前に毎回、待ち受けているのがサービスかを確かめる（core/service_endpoint.py。12章 22）
- 通信は QNetworkAccessManager で行い、画面のスレッドを止めない。結果は ApiReply.finished で受け取る
- 401 はサービスの再起動でトークンが変わったとき。トークンを読み直して 1 回だけやり直す
- admin のトークンはここでは扱わない（admin の操作は昇格ヘルパーが行う。12-4c）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QByteArray, QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

from core import exceptions
from core.exceptions import (
    AppError,
    ServicePermissionError,
    ServiceUnavailableError,
    ServiceVersionMismatchError,
)
from core.service_endpoint import read_endpoint, verify_service_listener

logger = logging.getLogger("clamav_frontend")

API_PREFIX = "/api/v1"
#: このクライアントが話せる API の版数（api/routes/info.py の API_VERSION と同じ。テストで確かめる）
API_VERSION = "v1"
DEFAULT_PORT = 8765
TOKEN_FILE = "user.token"
#: 1 回の要求を待つ長さの既定（ミリ秒）。スキャンなどの時間のかかる処理はジョブで受け付けるため短くてよい
DEFAULT_TIMEOUT_MS = 15_000
_MAX_TOKEN_SIZE = 256


def error_from_body(status: int, body: bytes) -> AppError:
    """エラー応答（{"error": {"code", "message", "detail"}}）を AppError にする。コードが分かれば、その種類の例外"""
    try:
        error = json.loads(body.decode("utf-8")).get("error", {})
        code, message, detail = error.get("code"), error.get("message"), error.get("detail")
    except (ValueError, AttributeError):
        code, message, detail = None, None, None
    if not isinstance(code, str):
        return exceptions.ApiError(detail=f"HTTP {status}")
    cls = _ERROR_CLASSES.get(code, AppError)
    return cls(message if isinstance(message, str) else None, code=code,
               detail=detail if isinstance(detail, str) else None)


def _error_classes() -> dict[str, type[AppError]]:
    """コード → 例外の種類（default_code を持つ AppError の派生のうち、最初に見つかったもの）"""
    found: dict[str, type[AppError]] = {}
    pending = [AppError]
    while pending:
        cls = pending.pop(0)
        code = getattr(cls, "default_code", None)
        if code and code not in found:
            found[code] = cls
        pending.extend(cls.__subclasses__())
    return found


_ERROR_CLASSES = _error_classes()


class ApiReply(QObject):
    """1 回の要求の結果。finished(data, error): 成功なら (応答の JSON（204 は None）, None)、失敗なら (None, AppError)"""

    finished = Signal(object, object)

    def __init__(self, client: "ApiClient") -> None:
        super().__init__(client)
        # 応答を待っている間にクライアント（と通信中の QNetworkReply）が消えないよう、参照を持っておく
        # （呼び出し側がクライアントを持っていないと、ガベージコレクションで消えてプロセスが落ちた）
        self._client = client


class ApiClient(QObject):
    def __init__(self, api_dir: Path, default_port: int = DEFAULT_PORT,
                 verify: Callable[[int], None] = verify_service_listener,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._api_dir = Path(api_dir)
        self._default_port = default_port
        self._verify = verify
        self._timeout_ms = timeout_ms
        self._nam = QNetworkAccessManager(self)
        self._token: str | None = None
        self.port = default_port

    @property
    def network(self) -> QNetworkAccessManager:
        return self._nam

    # -- 準備 -----------------------------------------------------------------------------------
    def prepare(self) -> None:
        """ポートとトークンを読む。トークンを読めなければ ServicePermissionError、無ければ ServiceUnavailableError"""
        self.port = read_endpoint(self._api_dir) or self._default_port
        path = self._api_dir / TOKEN_FILE
        try:
            with open(path, "rb") as f:
                token = f.read(_MAX_TOKEN_SIZE + 1)[:_MAX_TOKEN_SIZE].decode("ascii").strip()
        except PermissionError as exc:
            raise ServicePermissionError(detail=str(path)) from exc
        except FileNotFoundError as exc:
            # 未導入か、サービスが止まっている（止まるとトークンファイルを消す）
            raise ServiceUnavailableError(detail="サービスが動いていません") from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise ServiceUnavailableError(detail=f"トークンを読めません ({exc})") from exc
        if not token:
            raise ServiceUnavailableError(detail="トークンが空です")
        self._token = token

    def prepare_later(self) -> None:
        """次の要求の前にポートとトークンを読み直す（サービスが起動し直したとき）"""
        self._token = None

    def url(self, path: str) -> QUrl:
        return QUrl(f"http://127.0.0.1:{self.port}{API_PREFIX}{path}")

    def authorized_request(self, path: str) -> QNetworkRequest:
        """接続先を確かめてから、トークンを付けた要求を作る。確かめられなければ ServiceUnavailableError"""
        if self._token is None:
            self.prepare()
        self._verify(self.port)
        request = QNetworkRequest(self.url(path))
        request.setRawHeader(b"Authorization", f"Bearer {self._token}".encode("ascii"))
        return request

    # -- 要求 -----------------------------------------------------------------------------------
    def request(self, method: str, path: str, body=None, timeout_ms: int | None = None) -> ApiReply:
        reply = ApiReply(self)
        self._send(reply, method, path, body, timeout_ms, retried=False)
        return reply

    def get(self, path: str) -> ApiReply:
        return self.request("GET", path)

    def post(self, path: str, body=None) -> ApiReply:
        return self.request("POST", path, body)

    def check_version(self) -> ApiReply:
        """GET /version で、話せる版数かを確かめる。違えば ServiceVersionMismatchError"""
        result = ApiReply(self)

        def done(data, error):
            if error is None and (not isinstance(data, dict) or data.get("api_version") != API_VERSION):
                error = ServiceVersionMismatchError(detail=f"サービスの API: {data!r}"[:200])
            result.finished.emit(data if error is None else None, error)

        self.request("GET", "/version").finished.connect(done)
        return result

    def _send(self, reply: ApiReply, method: str, path: str, body, timeout_ms, retried: bool) -> None:
        try:
            request = self.authorized_request(path)
        except AppError as exc:
            # 呼び出し側が finished をつないでから知らせる（except を出ると exc の名前は消えるため、値を渡しておく）
            QTimer.singleShot(0, lambda error=exc: reply.finished.emit(None, error))
            return
        request.setTransferTimeout(timeout_ms or self._timeout_ms)
        data = QByteArray()
        if body is not None:
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            data = QByteArray(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        network_reply = self._nam.sendCustomRequest(request, method.encode("ascii"), data)

        def finished():
            network_reply.deleteLater()
            status = network_reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            payload = bytes(network_reply.readAll().data())
            if status is None:
                # つながらない・待ちきれない（サービスの停止・再起動中）
                error = ServiceUnavailableError(detail=network_reply.errorString())
                reply.finished.emit(None, error)
                return
            if status == 401 and not retried:
                # サービスが起動し直してトークンが変わった。読み直して 1 回だけやり直す
                self.prepare_later()
                self._send(reply, method, path, body, timeout_ms, retried=True)
                return
            if 200 <= status < 300:
                try:
                    result = json.loads(payload.decode("utf-8")) if payload else None
                except ValueError:
                    reply.finished.emit(None, exceptions.ApiError(detail="応答を読めません"))
                    return
                reply.finished.emit(result, None)
                return
            reply.finished.emit(None, error_from_body(status, payload))

        network_reply.finished.connect(finished)


def is_unreachable(error) -> bool:
    """サービスに接続できない種類のエラーか（トレイをエラーの状態にして、つなぎ直す）"""
    return isinstance(error, ServiceUnavailableError)
