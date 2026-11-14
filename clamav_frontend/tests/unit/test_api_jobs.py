"""ジョブ型の API とイベント配信のテスト（サービス化・Web API設計書 4.3節・5章。段階 12-2b）"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PySide6.QtCore import QObject, Signal

from api.app import API_PREFIX, create_app
from api.auth import ROLE_USER, ApiTokens
from api.bridge import EventHub
from api.context import ApiContext
from api.events import StatusPublisher, connect_realtime
from api.jobs import JobTracker
from core.exceptions import ClamdConnectionError
from core.update_service import UpdateHistoryRecord


def _events_of(hub: EventHub, name: str | None = None) -> list:
    return [e for e in hub.events_since(0) if name is None or e.name == name]


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


# -- JobTracker ---------------------------------------------------------------------------
@pytest.fixture
def tracker():
    hub = EventHub()
    clock = _Clock()
    return SimpleNamespace(hub=hub, clock=clock, jobs=JobTracker(hub, clock=clock))


def test_scan_job_lifecycle(tracker):
    jobs = tracker.jobs
    assert jobs.current("scan") is None
    jobs.on_scan_started("クイックスキャン")
    job = jobs.current("scan")
    assert job["state"] == "running" and job["label"] == "クイックスキャン" and job["source"] == "gui"
    assert jobs.running_job_id("scan") == job["id"]

    jobs.on_scan_file("/a/ok.txt", "OK", "", "")
    jobs.on_scan_file("/a/bad.exe", "FOUND", "Eicar", "2026-11-01T00:00:00+00:00")
    jobs.on_scan_file("/a/locked", "ERROR", "Permission denied", "")
    jobs.on_scan_finished({"scanned": 3, "detected": 1, "errors": 1, "cancelled": False})

    done = jobs.current("scan")
    assert done["state"] == "finished" and done["finished_at"]
    assert done["progress"] == {"scanned": 3, "total": 0, "detected": 1, "errors": 1}
    assert jobs.running_job_id("scan") is None
    # 検知とエラーだけを scan.file で知らせる（OK のファイルは送らない）
    assert [e.data["status"] for e in _events_of(tracker.hub, "scan.file")] == ["FOUND", "ERROR"]
    finished = _events_of(tracker.hub, "scan.finished")[0].data
    assert finished == {"job_id": job["id"], "state": "finished", "scanned": 3, "detected": 1, "errors": 1,
                        "cancelled": False, "history_id": None}


def test_progress_events_are_thinned_out(tracker):
    # 1秒に最大2回に間引く（ジョブの進み具合そのものは毎回更新する）
    jobs = tracker.jobs
    jobs.on_scan_started("手動スキャン")
    for i in range(10):
        jobs.on_scan_progress(i, 10, f"/f{i}")
        tracker.clock.now += 0.125
    sent = [e.data["scanned"] for e in _events_of(tracker.hub, "scan.progress")]
    assert sent == [0, 4, 8]
    assert jobs.current("scan")["progress"]["scanned"] == 9


def test_cancelled_and_failed_scans(tracker):
    jobs = tracker.jobs
    jobs.on_scan_started("手動スキャン")
    jobs.on_scan_finished({"scanned": 1, "detected": 0, "errors": 0, "cancelled": True})
    assert jobs.current("scan")["state"] == "cancelled"

    jobs.on_scan_started("手動スキャン")
    jobs.on_scan_error(ClamdConnectionError())
    jobs.on_scan_finished({"scanned": 0, "detected": 0, "errors": 0, "cancelled": False})
    job = jobs.current("scan")
    assert job["state"] == "failed" and job["error"]["code"] == "E1001"


def test_single_file_error_does_not_fail_the_job(tracker):
    # 1ファイルの応答待ちのタイムアウトなどではスキャンを続けるため、ジョブも失敗にしない
    from core.exceptions import ClamdTimeoutError

    jobs = tracker.jobs
    jobs.on_scan_started("手動スキャン")
    jobs.on_scan_progress(0, 2, "/a/slow.bin")
    jobs.on_scan_error(ClamdTimeoutError())
    jobs.on_scan_finished({"scanned": 2, "detected": 0, "errors": 1, "cancelled": False})
    assert jobs.current("scan")["state"] == "finished"


def test_scan_finished_carries_the_history_id(tracker):
    jobs = JobTracker(tracker.hub, last_scan_history_id=lambda: 42)
    jobs.on_scan_started("手動スキャン")
    jobs.on_scan_finished({"scanned": 0, "detected": 0, "errors": 0, "cancelled": False})
    assert jobs.current("scan")["history_id"] == 42
    assert _events_of(tracker.hub, "scan.finished")[-1].data["history_id"] == 42


def test_job_source_is_recorded(tracker):
    tracker.jobs.expect_source("scan", "api")
    tracker.jobs.on_scan_started("フルスキャン")
    assert tracker.jobs.current("scan")["source"] == "api"
    tracker.jobs.on_scan_finished({"scanned": 0, "detected": 0, "errors": 0, "cancelled": False})
    tracker.jobs.on_scan_started("フルスキャン")  # 次の（画面から始めた）ジョブには引き継がない
    assert tracker.jobs.current("scan")["source"] == "gui"


def test_event_text_keeps_one_record_per_line(tracker):
    # ファイル名・freshclam の出力の改行などで、1件のイベントが複数行に見えないこと
    jobs = tracker.jobs
    jobs.on_scan_started("手動スキャン")
    jobs.on_scan_file("/a/x\ny.exe", "FOUND", "Eicar\r\nevent: forged", "")
    jobs.on_update_started()
    jobs.on_update_log("line\nevent: forged")
    for event in _events_of(tracker.hub):
        assert "\n" not in str(event.data) and "\r" not in str(event.data)


def test_update_job(tracker):
    jobs = tracker.jobs
    jobs.on_update_started()
    jobs.on_update_log("Downloading daily.cvd")
    jobs.on_update_finished(UpdateHistoryRecord(1, "2026-11-01T00:00:00+00:00", "failure", "1", "1",
                                                detail="ERROR: x"))
    job = jobs.current("update")
    assert job["state"] == "failed" and job["progress"]["lines"] == 1 and job["error"]["code"] == "E8004"
    assert _events_of(tracker.hub, "update.finished")[0].data["record"]["result"] == "failure"


def test_dispatcher_announces_start(qtbot):
    # 画面・トレイから始めたスキャンも記録できるよう、開始を知らせる
    from core.scan_dispatcher import ScanDispatcher

    dispatcher = ScanDispatcher(SimpleNamespace())
    with qtbot.waitSignal(dispatcher.started) as started, qtbot.waitSignal(dispatcher.finished, timeout=5000):
        dispatcher.start([], label="手動スキャン")
    assert started.args == ["手動スキャン"]


# -- イベントの発生元 -------------------------------------------------------------------------
class _RealtimeSignals(QObject):
    detected = Signal(str, str)
    detection_handled = Signal(str, str, bool)
    watch_target_lost = Signal(str)
    scan_error = Signal(object)
    mode_name = "watchdog監視"


def test_realtime_events(qapp):
    hub = EventHub()
    manager = _RealtimeSignals()
    connect_realtime(hub, manager)
    manager.detected.emit("/w/a.exe", "Eicar")
    manager.detection_handled.emit("/w/a.exe", "Eicar", True)
    manager.watch_target_lost.emit("/w")
    manager.scan_error.emit(ClamdConnectionError())
    assert [(e.name, e.data) for e in _events_of(hub)] == [
        ("realtime.detected", {"path": "/w/a.exe", "virus_name": "Eicar"}),
        ("realtime.handled", {"path": "/w/a.exe", "virus_name": "Eicar", "quarantined": True}),
        ("realtime.target_lost", {"path": "/w"}),
        ("realtime.error", {"code": "E1001", "message": ClamdConnectionError.default_message,
                            "reason": "clamd_unavailable"}),
    ]


def test_realtime_error_reason_without_details(qapp):
    # 理由は決まった言葉だけで知らせ、detail（clamonacc の出力の一部・パス）は送らない
    from core.exceptions import ClamonaccError

    hub = EventHub()
    manager = _RealtimeSignals()
    connect_realtime(hub, manager)
    error = ClamonaccError(detail="管理者認証がキャンセルされたか、失敗しました / /home/a/secret")
    error.reason = "auth_cancelled"
    manager.scan_error.emit(error)
    data = _events_of(hub, "realtime.error")[0].data
    assert data["reason"] == "auth_cancelled"
    assert "secret" not in str(data)


def test_status_event_only_when_something_changes():
    hub = EventHub()
    jobs = JobTracker(hub)
    publisher = StatusPublisher(hub, SimpleNamespace(mode_name="clamonacc"), SimpleNamespace(is_running=False),
                                SimpleNamespace(is_running=False), jobs)
    status = SimpleNamespace(clamd_running=True, definitions_age_days=1, definitions_stale=False,
                             realtime_running=False)
    publisher.on_status(status)
    publisher.on_status(status)  # 2秒ごとの確認で変わっていなければ出さない
    status.realtime_running = True
    publisher.on_status(status)
    sent = _events_of(hub, "status")
    assert [e.data["realtime"]["running"] for e in sent] == [False, True]


# -- ルート -----------------------------------------------------------------------------------
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


class _FakeScanDispatcher:
    def __init__(self, jobs):
        self.jobs = jobs
        self.is_running = False
        self.started = []
        self.stopped = 0

    def start(self, targets, include_subfolders=True, apply_exclude=True, label="", skip_dirs=(), **kwargs):
        self.is_running = True
        self.started.append((targets, include_subfolders, apply_exclude, label, skip_dirs))
        self.kwargs = kwargs
        self.jobs.on_scan_started(label)

    def stop(self):
        self.stopped += 1


class _FakeUpdateDispatcher:
    def __init__(self, jobs):
        self.jobs = jobs
        self.is_running = False
        self.count = 0

    def start(self):
        self.is_running = True
        self.count += 1
        self.jobs.on_update_started()


@pytest.fixture
def env(tmp_path, monkeypatch):
    hub = EventHub()
    jobs = JobTracker(hub)
    scan = _FakeScanDispatcher(jobs)
    update = _FakeUpdateDispatcher(jobs)
    changed = []
    monkeypatch.setattr("core.scan_targets.quick_scan_targets", lambda: [str(tmp_path)])
    monkeypatch.setattr("core.scan_targets.full_scan_targets", lambda: ["/"])
    monkeypatch.setattr("core.scan_targets.full_scan_skip_dirs", lambda: ("/proc",))
    context = ApiContext(
        config_service=None, clamav_service=None, realtime_manager=None, scan_dispatcher=scan,
        update_service=None, update_dispatcher=update, scheduler_service=None, quarantine_service=None,
        exclude_service=None, watch_target_service=None, log_reader_service=None,
        invoker=_DirectInvoker(), events=hub, jobs=jobs, notify_changed=changed.append,
    )
    tokens = ApiTokens(tmp_path / "api_tokens")
    client = TestClient(create_app(tokens, context), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    return SimpleNamespace(client=client, tmp=tmp_path, scan=scan, update=update, jobs=jobs, hub=hub,
                           changed=changed, context=context, tokens=tokens,
                           user={"Authorization": f"Bearer {tokens.token_for(ROLE_USER)}"})


def _call(env, method, path, json=None, headers=None):
    return env.client.request(method, f"{API_PREFIX}{path}", json=json,
                              headers=env.user if headers is None else headers)


@pytest.mark.parametrize("method,path", [("POST", "/scans"), ("GET", "/scans/current"),
                                         ("POST", "/scans/current/cancel"), ("POST", "/updates"),
                                         ("GET", "/updates/current"), ("GET", "/events")])
def test_job_apis_need_a_token(env, method, path):
    assert _call(env, method, path, {"type": "quick"}, headers={}).status_code == 401
    assert env.scan.started == [] and env.update.count == 0


def test_start_custom_scan(env):
    response = _call(env, "POST", "/scans", {"type": "custom", "targets": [str(env.tmp)], "apply_exclude": False})
    assert response.status_code == 202
    job = response.json()["job"]
    assert job["state"] == "running" and job["source"] == "api" and job["label"] == "カスタムスキャン"
    assert env.scan.started == [([str(env.tmp)], True, False, "カスタムスキャン", ())]
    assert env.scan.kwargs == {"source": "api", "scan_type": "custom"}  # スキャン履歴に残す出どころと種類
    assert env.changed == ["scan_started"]  # 画面を実行中の表示にする
    assert _call(env, "GET", "/scans/current").json()["job"]["id"] == job["id"]
    # 実行中は受け付けない
    assert _call(env, "POST", "/scans", {"type": "quick"}).status_code == 409


def test_quick_and_full_scan_targets(env):
    _call(env, "POST", "/scans", {"type": "quick"})
    env.scan.is_running = False
    _call(env, "POST", "/scans", {"type": "full"})
    assert [(s[0], s[3], s[4]) for s in env.scan.started] == [
        ([str(env.tmp)], "クイックスキャン", ()), (["/"], "フルスキャン", ("/proc",))]


@pytest.mark.parametrize("body", [
    {"type": "custom"},
    {"type": "custom", "targets": ["relative/dir"]},
    {"type": "custom", "targets": ["/no/such/path/for/clamqtpie"]},
    {"type": "virus"},
    {"type": "custom", "targets": "/tmp"},
    {"type": "quick", "include_subfolders": "yes"},
])
def test_invalid_scan_requests_are_400(env, body):
    assert _call(env, "POST", "/scans", body).status_code == 400
    assert env.scan.started == []


def test_cancel_scan(env):
    assert _call(env, "POST", "/scans/current/cancel").status_code == 409  # 実行中のスキャンが無い
    _call(env, "POST", "/scans", {"type": "quick"})
    assert _call(env, "POST", "/scans/current/cancel").status_code == 202
    assert env.scan.stopped == 1


def test_start_update(env):
    assert _call(env, "GET", "/updates/current").json() == {"job": None}
    response = _call(env, "POST", "/updates")
    assert response.status_code == 202
    assert response.json()["job"]["kind"] == "update"
    assert env.changed == ["update_started"]
    assert _call(env, "POST", "/updates").status_code == 409
    assert env.update.count == 1


def test_too_many_event_subscribers_are_refused(env, monkeypatch):
    monkeypatch.setattr("api.routes.jobs.MAX_SUBSCRIBERS", 0)
    assert _call(env, "GET", "/events").status_code == 409


def test_events_stream_and_resend_after_reconnect(env):
    # 実際の待ち受けで、再接続のとき Last-Event-ID より後のイベントを送り直し、その後のイベントも届く
    import httpx2 as httpx

    from api.server import LOOPBACK_HOST, ApiServer

    first = env.hub.publish("scan.started", {"n": 1})
    env.hub.publish("scan.progress", {"n": 2})
    server = ApiServer(create_app(env.tokens, env.context), 0)
    server.start()
    received = []
    try:
        with httpx.stream("GET", f"http://{LOOPBACK_HOST}:{server.port}{API_PREFIX}/events",
                          headers={**env.user, "Last-Event-ID": str(first.id)}, timeout=10) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")

            def publish_later():
                time.sleep(0.3)
                env.hub.publish("scan.finished", {"n": 3})

            threading.Thread(target=publish_later, daemon=True).start()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    received.append(line)
                if len(received) == 2:
                    break
    finally:
        server.stop()
    assert received == ['data: {"n": 2}', 'data: {"n": 3}']
    assert env.hub.subscriber_count == 0  # 切断したら購読をやめる


def test_stopping_with_an_open_subscription_ends_it_cleanly(env, monkeypatch):
    # 購読（SSE）がつながったままアプリを終了しても、配信を自分で終わらせ、uvicorn に打ち切らせない
    # （打ち切られるとログにエラーが残り、終了も遅れる。Linux 実機の確認で発見）
    import httpx2 as httpx

    from api import server as server_module
    from api.server import LOOPBACK_HOST, ApiServer

    logged = []
    monkeypatch.setattr(server_module, "logger", SimpleNamespace(
        log=lambda level, message, **kwargs: logged.append(message)))
    server = ApiServer(create_app(env.tokens, env.context), 0)
    server.start()
    connected = threading.Event()
    ended = threading.Event()

    def subscribe():
        try:
            with httpx.stream("GET", f"http://{LOOPBACK_HOST}:{server.port}{API_PREFIX}/events",
                              headers=env.user, timeout=10) as response:
                connected.set()
                for _ in response.iter_lines():
                    pass
        except httpx.HTTPError:
            pass
        ended.set()

    threading.Thread(target=subscribe, daemon=True).start()
    assert connected.wait(5)
    deadline = time.monotonic() + 5
    while env.hub.subscriber_count == 0 and time.monotonic() < deadline:
        time.sleep(0.02)

    started = time.monotonic()
    server.stop()
    assert time.monotonic() - started < 0.9  # 打ち切りの待ち（1 秒）まで待たない
    assert ended.wait(5)  # クライアント側も配信の終わりを受け取る
    assert logged == []


def test_events_after_a_restart_reach_a_client_with_an_old_last_event_id(env):
    # 起動し直す前のサービスの番号（今の番号より大きい）で再接続しても、新しいイベントが届く
    import httpx2 as httpx

    from api.server import LOOPBACK_HOST, ApiServer

    env.hub.publish("scan.started", {"n": 1})  # 送り直さない（このサービスの前の番号ではないため）
    stale_id = env.hub.latest_id + 10_000
    server = ApiServer(create_app(env.tokens, env.context), 0)
    server.start()
    received = []
    try:
        with httpx.stream("GET", f"http://{LOOPBACK_HOST}:{server.port}{API_PREFIX}/events",
                          headers={**env.user, "Last-Event-ID": str(stale_id)}, timeout=10) as response:
            assert response.status_code == 200

            def publish_later():
                time.sleep(0.3)
                env.hub.publish("scan.finished", {"n": 2})

            threading.Thread(target=publish_later, daemon=True).start()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    received.append(line)
                    break
    finally:
        server.stop()
    assert received == ['data: {"n": 2}']
