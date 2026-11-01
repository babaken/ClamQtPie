"""Web API の待ち受けのテスト（サービス化・Web API設計書 3.1節）"""
from __future__ import annotations

import socket
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from fastapi import Request

from api.app import create_app
from api.auth import ApiTokens
from api.server import LOOPBACK_HOST, ApiServer, create_listen_socket, start_api_server


def test_listen_socket_accepts_only_loopback():
    # 内部 API は LAN に出さない
    with pytest.raises(ValueError):
        create_listen_socket(0, host="0.0.0.0")
    sock = create_listen_socket(0)
    try:
        assert sock.getsockname()[0] == LOOPBACK_HOST
    finally:
        sock.close()


@pytest.mark.parametrize("reuse", [True, False])
def test_another_socket_cannot_take_over_the_port(reuse):
    # 待ち受け中のアドレス・ポートに、別のソケットが割り込めないこと（SO_REUSEADDR を付けても）
    sock = create_listen_socket(0)
    port = sock.getsockname()[1]
    intruder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if reuse:
            intruder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            intruder.bind((LOOPBACK_HOST, port))
            intruder.listen(1)
    finally:
        intruder.close()
        sock.close()


def test_requests_reach_the_server_even_if_wildcard_is_taken(tmp_path):
    # 同じポートを 0.0.0.0 で待ち受けられても（OS によっては許される）、127.0.0.1 への接続は
    # より限定したアドレスで待ち受けているこちらに届くこと
    server = ApiServer(create_app(ApiTokens(tmp_path)), 0)
    server.start()
    intruder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            intruder.bind(("0.0.0.0", server.port))
            intruder.listen(1)
        except OSError:
            pytest.skip("この OS では 0.0.0.0 でも待ち受けられない（割り込めない）")
        response = httpx.get(f"http://{LOOPBACK_HOST}:{server.port}/api/v1/version", timeout=5)
        assert response.status_code == 200
    finally:
        intruder.close()
        server.stop()


def test_server_answers_and_stops(tmp_path):
    server = ApiServer(create_app(ApiTokens(tmp_path)), 0)
    server.start()
    try:
        assert server.is_running
        response = httpx.get(f"http://{LOOPBACK_HOST}:{server.port}/api/v1/version", timeout=5)
        assert response.status_code == 200
        assert "server" not in response.headers  # サーバーの種類を名乗らない
    finally:
        port = server.port
        server.stop()
    assert not server.is_running
    # 止めたらポートを手放している（もう一度待ち受けられる）
    create_listen_socket(port).close()


def test_start_api_server_does_nothing_when_disabled(tmp_path):
    assert start_api_server(SimpleNamespace(enabled=False, port=8765), create_app, tmp_path) is None
    assert not (tmp_path / "api_tokens").exists()


def test_start_api_server_continues_when_port_is_in_use(tmp_path):
    # ポートを使えなくてもアプリは止めない（ログに残して None を返す）。トークンファイルも残さない
    blocker = create_listen_socket(0)
    try:
        port = blocker.getsockname()[1]
        assert start_api_server(SimpleNamespace(enabled=True, port=port), create_app, tmp_path) is None
    finally:
        blocker.close()
    assert list((tmp_path / "api_tokens").glob("*.token")) == []


def test_token_files_exist_only_while_running(tmp_path):
    server = start_api_server(SimpleNamespace(enabled=True, port=0), create_app, tmp_path)
    assert server is not None
    token_dir = tmp_path / "api_tokens"
    try:
        token = (token_dir / "user.token").read_text(encoding="ascii")
        response = httpx.get(f"http://{LOOPBACK_HOST}:{server.port}/api/v1/version",
                             headers={"Authorization": f"Bearer {token}"}, timeout=5)
        assert response.status_code == 200
    finally:
        server.stop()
    assert list(token_dir.glob("*.token")) == []



def test_forwarded_headers_do_not_change_the_client(tmp_path):
    # X-Forwarded-For で、別のマシンからの要求に見せかけられない（接続の相手で判断する）
    app = create_app(ApiTokens(tmp_path))
    seen = []

    def record(request: Request):
        seen.append(request.client.host)
        return {}

    app.add_api_route("/api/v1/_client", record, methods=["GET"])
    server = ApiServer(app, 0)
    server.start()
    try:
        response = httpx.get(f"http://{LOOPBACK_HOST}:{server.port}/api/v1/_client",
                             headers={"X-Forwarded-For": "203.0.113.5"}, timeout=5)
        assert response.status_code == 200
    finally:
        server.stop()
    assert seen == [LOOPBACK_HOST]


def test_stopping_the_server_is_logged(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr("api.server.log_event", lambda category, message: events.append(message))
    server = ApiServer(create_app(ApiTokens(tmp_path)), 0)
    server.start()
    server.stop()
    server.stop()  # 2回止めても1回だけ残す
    assert events == ["Web API の待ち受けを停止しました"]


def test_uvicorn_warnings_go_to_the_app_log(monkeypatch):
    # uvicorn の警告（不正な要求など）もアプリのログに残す。1件の記録が複数行に分かれない
    import logging

    from api import server as server_module

    records = []
    monkeypatch.setattr(server_module, "logger", SimpleNamespace(
        log=lambda level, message, **kwargs: records.append((level, message, kwargs["extra"]["error_code"]))))
    server_module._route_uvicorn_logs()
    server_module._route_uvicorn_logs()  # 何度呼んでも1回だけ書く
    logging.getLogger("uvicorn.error").info("Started server process")
    logging.getLogger("uvicorn.error").warning("Invalid HTTP request received.\nforged line")
    assert records == [(logging.WARNING, r"Web API（uvicorn）: Invalid HTTP request received.\nforged line", "E8100")]
