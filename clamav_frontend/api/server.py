"""
Web API の待ち受け（サービス化・Web API設計書 3.1節・8章）

- 内部 API が待ち受けるのは 127.0.0.1 だけ（LAN には出さない）。WebUI は、LAN からの接続を許可しているときだけ、
  ループバックに加えて LAN の各アドレスでも待ち受ける（hosts。段階 12-5e2。どれも同じポート）
- ソケットは自分で作って uvicorn に渡す。Windows では SO_EXCLUSIVEADDRUSE を付け、同じポートに
  別のプロセスが割り込めないようにする（SO_REUSEADDR を付けたソケットや、より限定したアドレスでの bind を防ぐ）
- uvicorn は専用のスレッドの asyncio ループで動かす。Qt のメインスレッドは止めない
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

from api.auth import TOKEN_DIR_NAME, ApiTokens
from core.logger import CATEGORY_SYSTEM, log_event
from core.text_safety import visible_controls

logger = logging.getLogger("clamav_frontend")

#: 内部 API の待ち受けアドレス（変更不可）
LOOPBACK_HOST = "127.0.0.1"

_START_TIMEOUT_SEC = 5.0
_STOP_TIMEOUT_SEC = 5.0


class _UvicornLogHandler(logging.Handler):
    """uvicorn の警告・エラー（不正な HTTP の要求、処理中の例外など）をアプリのログに書く。
    uvicorn のロガーはアプリのロガーと別で、log_config=None のままでは どこにも残らないため"""

    def emit(self, record: logging.LogRecord) -> None:
        logger.log(
            record.levelno,
            f"Web API（uvicorn）: {visible_controls(record.getMessage())}",
            exc_info=record.exc_info,
            extra={"error_code": "E8100"},
        )


def _route_uvicorn_logs() -> None:
    uvicorn_logger = logging.getLogger("uvicorn")
    if not any(isinstance(h, _UvicornLogHandler) for h in uvicorn_logger.handlers):
        uvicorn_logger.addHandler(_UvicornLogHandler(logging.WARNING))
    uvicorn_logger.setLevel(logging.WARNING)  # 起動・停止の案内（INFO）は残さない（こちらで記録する）
    uvicorn_logger.propagate = False


def create_listen_socket(port: int, host: str = LOOPBACK_HOST, allow_lan: bool = False) -> socket.socket:
    """待ち受けのソケットを作る。ループバック以外のアドレスは、allow_lan（WebUI の LAN からの接続）のときに、
    LAN として扱うアドレス（core.net_scope.is_lan_address）だけを受け付ける（0.0.0.0 などは受け付けない）"""
    if host != LOOPBACK_HOST:
        import ipaddress

        from core.net_scope import is_lan_address

        try:
            lan = allow_lan and is_lan_address(ipaddress.IPv4Address(host))
        except ValueError:
            lan = False
        if not lan:
            raise ValueError(f"{LOOPBACK_HOST} か LAN のアドレスでだけ待ち受けます: {host}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # Linux 等: 再起動直後（TIME_WAIT）でも待ち受けられるようにする。待ち受け中のポートへの
            # 割り込みは、SO_REUSEPORT を付けない限り OS が拒否する
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
        sock.set_inheritable(False)
    except OSError:
        sock.close()
        raise
    return sock


class ApiServer:
    """uvicorn をスレッドで動かす。start() で待ち受けを始め、stop() で止める"""

    def __init__(self, app, port: int, tokens: ApiTokens | None = None, name: str = "ApiServer",
                 event_tag: str = "internal", tls: tuple[str, str] | None = None,
                 hosts: list[str] | None = None) -> None:
        """name: スレッドの名前（WebUI の待ち受けは "WebUI"）。event_tag: 止めるときに終わらせるイベントの購読の印
        （内部 API と WebUI の待ち受けは同じ EventHub を使うため、自分の待ち受けの購読だけを終わらせる）。
        tls: HTTPS で待ち受けるときの (サーバー証明書, 鍵) のファイル（WebUI だけ。段階 12-5e1）。
        hosts: 待ち受けるアドレス（WebUI の LAN からの接続だけ。先頭はループバック。省くとループバックだけ）"""
        self._app = app
        self._tls = tls
        self._hosts = list(hosts) if hosts else [LOOPBACK_HOST]
        self._extra_sockets: list[socket.socket] = []
        self._port = port
        self._tokens = tokens  # 止めたときにトークンファイルを消す
        self._name = name
        self._event_tag = event_tag
        self._socket: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._started = False  # 待ち受けを始められたか（止めたときにログに残すかどうか）

    @property
    def port(self) -> int:
        """実際に待ち受けているポート（0 を指定した場合は OS が選んだもの）"""
        return self._socket.getsockname()[1] if self._socket is not None else self._port

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """待ち受けを始める。ポートを使えない・起動しない場合は OSError / RuntimeError"""
        self._socket = create_listen_socket(self._port, self._hosts[0], allow_lan=len(self._hosts) > 1)
        try:
            # LAN のアドレス（ループバックと同じポート。0 を指定したときは OS が選んだもの）
            for host in self._hosts[1:]:
                self._extra_sockets.append(create_listen_socket(self.port, host, allow_lan=True))
        except (OSError, ValueError):
            self._close_sockets()
            raise
        _route_uvicorn_logs()
        config = uvicorn.Config(
            self._app,
            log_config=None,  # アプリのロガーの設定を変えない
            access_log=False,
            lifespan="off",
            server_header=False,
            # X-Forwarded-For 等で接続元を書き換えさせない（接続元の確認は実際の接続の相手で行う）
            proxy_headers=False,
            # 止めるときは、つながったままのイベントの購読（SSE）を待ち続けずに切る
            timeout_graceful_shutdown=1,
            # HTTPS（WebUI だけ）。TLS 1.2 より古いものは Python の既定の設定で受け付けない
            ssl_certfile=self._tls[0] if self._tls else None,
            ssl_keyfile=self._tls[1] if self._tls else None,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._socket, *self._extra_sockets]}, name=self._name,
            daemon=True
        )
        self._thread.start()
        if not self._wait_started():
            self.stop()
            raise RuntimeError("Web API の待ち受けを開始できませんでした")
        self._started = True

    def _wait_started(self) -> bool:
        deadline = time.monotonic() + _START_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self._server.started:
                return True
            if not self._thread.is_alive():
                return False
            time.sleep(0.05)
        return self._server.started

    def stop(self) -> None:
        was_started = self._started
        self._started = False
        # つながったままのイベントの購読（SSE）を先に終わらせる（uvicorn に打ち切らせると、ログにエラーが残り、
        # 終了も timeout_graceful_shutdown の分だけ遅れる）
        context = getattr(getattr(self._app, "state", None), "context", None)
        events = getattr(context, "events", None)
        if events is not None:
            events.end_all(self._event_tag)
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(_STOP_TIMEOUT_SEC)
        self._close_sockets()
        self._server = None
        self._thread = None
        if self._tokens is not None:
            self._tokens.remove_files()
        if was_started and self._name == "ApiServer":
            log_event(CATEGORY_SYSTEM, "Web API の待ち受けを停止しました")

    def _close_sockets(self) -> None:
        for sock in [self._socket, *self._extra_sockets]:
            if sock is not None:
                sock.close()
        self._socket = None
        self._extra_sockets = []


def start_api_server(api_config, app_factory, app_dir: Path) -> ApiServer | None:
    """設定（config.api）が有効なら待ち受けを始める。失敗してもアプリは続ける（ログに残す）

    サービスの本体（api/service_api.py）から呼ぶ。app_factory は ApiTokens を受け取って FastAPI のアプリを返す関数（api.app.create_app）。
    トークンファイルは app_dir/api_tokens/ に書き出し、待ち受けを止めたら消す
    """
    if not api_config.enabled:
        return None
    tokens = ApiTokens(Path(app_dir) / TOKEN_DIR_NAME)
    server = ApiServer(app_factory(tokens), api_config.port, tokens=tokens)
    try:
        tokens.write_files()
        server.start()
    except (OSError, RuntimeError) as exc:
        tokens.remove_files()
        logger.warning(
            f"Web API の待ち受けを開始できませんでした（{LOOPBACK_HOST}:{api_config.port}） (detail: {exc})",
            extra={"error_code": "E8100"},
        )
        return None
    log_event(CATEGORY_SYSTEM, f"Web API の待ち受けを開始しました（{LOOPBACK_HOST}:{server.port}）")
    return server
