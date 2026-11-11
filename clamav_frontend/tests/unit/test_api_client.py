"""トレイGUI の API クライアント（api_client/）のテスト（サービス化・Web API設計書 3.1節・5章・10章。段階 12-4a）

本物の FastAPI のアプリを uvicorn で待ち受けさせ、QNetworkAccessManager でつなぐ。
"""
from __future__ import annotations

import asyncio
import builtins
import concurrent.futures
import sys

import pytest

from api.app import create_app
from api.auth import ROLE_USER, ApiTokens
from api.bridge import EventHub
from api.context import ApiContext
from api.jobs import JobTracker
from api.server import ApiServer
from api_client import client as client_module
from api_client.client import ApiClient, error_from_body
from api_client.events import EventStream
from core.exceptions import (
    ApiError,
    ApiForbiddenError,
    ServicePermissionError,
    ServiceUnavailableError,
    ServiceVersionMismatchError,
)
from core.service_endpoint import write_endpoint


class _DirectInvoker:
    def call(self, fn, *args):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    async def run(self, fn, *args):
        return await asyncio.wrap_future(self.call(fn, *args))


class _Idle:
    is_running = False


@pytest.fixture
def service(tmp_path):
    api_dir = tmp_path / "api"
    tokens = ApiTokens(api_dir)
    hub = EventHub()
    context = ApiContext(
        config_service=None, clamav_service=None, realtime_manager=None, scan_dispatcher=_Idle(),
        update_service=None, update_dispatcher=_Idle(), scheduler_service=None, quarantine_service=None,
        exclude_service=None, watch_target_service=None, log_reader_service=None,
        invoker=_DirectInvoker(), events=hub, jobs=JobTracker(hub),
    )
    server = ApiServer(create_app(tokens, context), 0, tokens=tokens)
    tokens.write_files()
    server.start()
    write_endpoint(api_dir, server.port)
    yield type("Service", (), {"api_dir": api_dir, "tokens": tokens, "hub": hub, "server": server})
    server.stop()


def _client(service, verify=lambda port: None):
    client = ApiClient(service.api_dir, default_port=1, verify=verify)
    client.prepare()
    return client


def _wait(qtbot, reply, timeout=10000):
    with qtbot.waitSignal(reply.finished, timeout=timeout) as blocker:
        pass
    return blocker.args


def test_client_speaks_the_same_api_version():
    from api.routes.info import API_VERSION

    assert client_module.API_VERSION == API_VERSION


def test_port_and_token_come_from_the_service_files(service):
    client = _client(service)
    assert client.port == service.server.port  # endpoint.json
    assert client._token == service.tokens.token_for(ROLE_USER)


def test_request_with_the_user_token(qtbot, service):
    data, error = _wait(qtbot, _client(service).get("/scans/current"))
    assert error is None and data == {"job": None}


def test_error_response_becomes_the_matching_exception(qtbot, service):
    data, error = _wait(qtbot, _client(service).post("/scans", {"type": "bogus"}))
    assert data is None and isinstance(error, ApiError) and error.code == "E8100"
    forbidden = error_from_body(403, b'{"error": {"code": "E8102", "message": "m", "detail": null}}')
    assert isinstance(forbidden, ApiForbiddenError) and forbidden.message == "m"
    assert error_from_body(500, b"not json").code == "E8100"


def test_token_is_not_sent_to_an_unverified_listener(qtbot, service):
    def refuse(port):
        raise ServiceUnavailableError("接続先が ClamQtPie のサービスではありません")

    data, error = _wait(qtbot, _client(service, verify=refuse).get("/scans/current"))
    assert data is None and isinstance(error, ServiceUnavailableError)


def test_changed_token_is_read_again_once(qtbot, service):
    client = _client(service)
    client._token = "old-token"  # サービスが起動し直してトークンが変わった
    data, error = _wait(qtbot, client.get("/scans/current"))
    assert error is None and data == {"job": None}


def test_version_is_checked(qtbot, service, monkeypatch):
    data, error = _wait(qtbot, _client(service).check_version())
    assert error is None and data["api_version"] == "v1"
    monkeypatch.setattr(client_module, "API_VERSION", "v2")
    _, error = _wait(qtbot, _client(service).check_version())
    assert isinstance(error, ServiceVersionMismatchError)


def test_stopped_service_is_reported_as_unreachable(qtbot, service):
    client = _client(service)
    service.server.stop()
    _, error = _wait(qtbot, client.get("/scans/current"))
    assert isinstance(error, ServiceUnavailableError)


def test_missing_or_unreadable_token(tmp_path, monkeypatch):
    client = ApiClient(tmp_path, verify=lambda port: None)
    with pytest.raises(ServiceUnavailableError):
        client.prepare()  # サービスが動いていない（トークンファイルが無い）
    (tmp_path / "user.token").write_text("x", encoding="ascii")
    real_open = builtins.open

    def denied(path, *args, **kwargs):
        if str(path).endswith("user.token"):
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    with pytest.raises(ServicePermissionError) as error:
        client.prepare()  # グループに入っていない
    assert error.value.code == "E8110"


# -- イベント ----------------------------------------------------------------------------------
def test_events_arrive_as_signals(qtbot, service):
    stream = EventStream(_client(service), reconnect_ms=200)
    with qtbot.waitSignal(stream.connected, timeout=10000):
        stream.start()
    with qtbot.waitSignal(stream.event, timeout=10000) as blocker:
        service.hub.publish("notification", {"id": 1, "title": "検知"})
    assert blocker.args == ["notification", {"id": 1, "title": "検知"}]
    assert stream.last_event_id is not None
    stream.stop()


def test_reconnect_receives_the_missed_events(qtbot, service):
    service.hub.publish("status", {"n": 1})
    service.hub.publish("status", {"n": 2})
    stream = EventStream(_client(service), reconnect_ms=200)
    stream._last_id = service.hub.events_since(0)[0].id  # 1 件目までは受け取り済み
    received = []
    stream.event.connect(lambda name, data: received.append(data))
    stream.start()
    qtbot.waitUntil(lambda: received == [{"n": 2}], timeout=10000)
    stream.stop()


def test_disconnect_is_reported_once_and_retried(qtbot, service):
    stream = EventStream(_client(service), reconnect_ms=100)
    lost = []
    stream.disconnected.connect(lost.append)
    with qtbot.waitSignal(stream.connected, timeout=10000):
        stream.start()
    service.server.stop()
    qtbot.waitUntil(lambda: len(lost) == 1, timeout=10000)
    qtbot.wait(500)  # つなぎ直しを何度か試しても、知らせるのは 1 回
    assert len(lost) == 1 and isinstance(lost[0], ServiceUnavailableError)
    stream.stop()


def test_event_blocks_are_parsed():
    stream = EventStream.__new__(EventStream)
    EventStream.__init__(stream, ApiClient("/nonexistent", verify=lambda port: None))
    got = []
    stream.event.connect(lambda name, data: got.append((name, data)))
    stream._dispatch(b": keepalive")
    stream._dispatch(b'id: 7\nevent: scan.file\ndata: {"path": "a"}')
    stream._dispatch(b"event: broken\ndata: {")
    assert got == [("scan.file", {"path": "a"})] and stream.last_event_id == 7


# -- トレイGUI の多重起動防止 ------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows のミューテックス")
def test_tray_uses_a_per_session_mutex(monkeypatch):
    import win32event

    from core.single_instance import TRAY_MUTEX_NAME, SingleInstanceLock

    names = []
    real = win32event.CreateMutex
    monkeypatch.setattr(win32event, "CreateMutex", lambda sa, owner, name: names.append(name) or real(sa, owner, None))
    lock = SingleInstanceLock(mutex_name=TRAY_MUTEX_NAME)
    assert lock.acquire()
    lock.release()
    assert names == [TRAY_MUTEX_NAME] and TRAY_MUTEX_NAME.startswith("Local\\")
