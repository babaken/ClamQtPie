"""状態を変える API のテスト（サービス化・Web API設計書 6章。段階 12-2）"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import API_PREFIX, create_app
from api.auth import ROLE_ADMIN, ROLE_USER, ApiTokens
from api.context import ApiContext
from core.config_service import ConfigService
from core.diagnostics_service import DiagnosticsReport
from core.exceptions import ClamdProcessError
from core.exclude_service import ExcludeService
from core.log_reader_service import LogReaderService
from core.quarantine_service import QuarantineService
from core.scheduler_service import SchedulerService
from core.watch_target_service import WatchTargetService
from db.database import DatabaseService


class _DirectInvoker:
    """テスト用: メインスレッドに渡さずその場で実行する（TestClient の呼び出し中はメインスレッドが待っているため）"""

    def call(self, fn, *args):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    async def run(self, fn, *args):
        return await asyncio.wrap_future(self.call(fn, *args))


class _FakeRealtime:
    mode_name = "watchdog監視"

    def __init__(self):
        self.is_running = False
        self.can_start = True
        self.calls = []

    def start(self):
        self.calls.append("start")
        self.is_running = self.can_start
        return self.can_start

    def stop(self):
        self.calls.append("stop")
        self.is_running = False

    def add_target(self, path):
        self.calls.append(("add_target", path))

    def remove_target(self, path):
        self.calls.append(("remove_target", path))

    def on_excludes_changed(self, type_):
        self.calls.append(("excludes", type_))


class _FakeController:
    def __init__(self):
        self.fail = False
        self.stopped = 0

    def stop(self):
        if self.fail:
            raise ClamdProcessError(detail="systemctl stop に失敗")
        self.stopped += 1
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = DatabaseService(tmp_path / "app.db")
    db.connect()
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    passwords = {}
    monkeypatch.setattr(config_service, "get_smtp_password", lambda: passwords.get("smtp"))
    monkeypatch.setattr(config_service, "set_smtp_password", lambda p: passwords.__setitem__("smtp", p))
    monkeypatch.setattr(config_service, "delete_smtp_password", lambda: passwords.pop("smtp", None))
    log_path = tmp_path / "logs" / "app.log"
    log_path.parent.mkdir()
    log_path.write_text("2026-10-31 10:00:00,000 [INFO] <スキャン> 行\n", encoding="utf-8")

    scanned = []
    run_started = threading.Event()
    release_run = threading.Event()

    def run_scan(target, name):
        scanned.append((target, name))
        run_started.set()
        release_run.wait(5)
        return SimpleNamespace(detected=0)

    scheduler = SchedulerService(db, run_scan, lambda: SimpleNamespace(result="success"))
    realtime = _FakeRealtime()
    changed = []
    applied = []
    stop_hooks = []
    clamd_starts = {"ok": True, "count": 0}

    def start_clamd():
        clamd_starts["count"] += 1
        return clamd_starts["ok"]

    report = DiagnosticsReport(created_at=datetime(2026, 11, 1))
    report.sections.append(("接続", [("ソケット", "/run/secret/clamd.ctl")]))
    report.sensitive["/run/secret/clamd.ctl"] = "<socket>"
    mails = []
    controller = _FakeController()

    context = ApiContext(
        config_service=config_service,
        clamav_service=SimpleNamespace(ping=lambda retry=True: False),
        realtime_manager=realtime,
        scan_dispatcher=SimpleNamespace(is_running=False),
        update_service=SimpleNamespace(last_executed_at=lambda: None, list_history=lambda: []),
        update_dispatcher=SimpleNamespace(is_running=False),
        scheduler_service=scheduler,
        quarantine_service=QuarantineService(db, tmp_path / "quarantine"),
        exclude_service=ExcludeService(db),
        watch_target_service=WatchTargetService(db),
        log_reader_service=LogReaderService(log_path),
        invoker=_DirectInvoker(),
        notification_service=SimpleNamespace(send_test_mail=lambda: mails.append(1)),
        diagnostics_service=SimpleNamespace(run=lambda: report),
        notify_changed=changed.append,
        apply_config=applied.append,
        start_clamd=start_clamd,
        clamd_controller=controller,
        on_clamd_stop_requested=lambda: stop_hooks.append("requested"),
        on_clamd_stop_failed=lambda: stop_hooks.append("failed"),
    )
    tokens = ApiTokens(tmp_path / "api_tokens")
    client = TestClient(create_app(tokens, context), base_url="http://127.0.0.1:8765",
                        client=("127.0.0.1", 50000), raise_server_exceptions=False)
    yield SimpleNamespace(
        client=client, tmp=tmp_path, context=context, realtime=realtime, changed=changed, applied=applied,
        passwords=passwords, scanned=scanned, run_started=run_started, release_run=release_run,
        clamd_starts=clamd_starts, stop_hooks=stop_hooks, controller=controller, mails=mails,
        admin={"Authorization": f"Bearer {tokens.token_for(ROLE_ADMIN)}"},
        user={"Authorization": f"Bearer {tokens.token_for(ROLE_USER)}"},
    )
    release_run.set()
    db.close()


def _call(env, method, path, json=None, headers=None):
    return env.client.request(method, f"{API_PREFIX}{path}", json=json,
                              headers=env.admin if headers is None else headers)


def _error_code(response):
    return response.json()["error"]["code"]


# -- 権限 -------------------------------------------------------------------------------
_WRITE_CALLS = [
    ("POST", "/quarantine/1/restore", {}),
    ("DELETE", "/quarantine/1", None),
    ("POST", "/excludes", {"type": "extension", "value": ".log"}),
    ("PATCH", "/excludes/1", {"enabled": False}),
    ("DELETE", "/excludes/1", None),
    ("PUT", "/realtime", {"enabled": True}),
    ("POST", "/realtime/targets", {"path": "/x"}),
    ("PATCH", "/realtime/targets/1", {"enabled": False}),
    ("DELETE", "/realtime/targets/1", None),
    ("POST", "/schedules", {"name": "a", "scan_type": "update", "cron": "0 3 * * *"}),
    ("PUT", "/schedules/1", {"name": "a", "scan_type": "update", "cron": "0 3 * * *"}),
    ("PATCH", "/schedules/1", {"enabled": False}),
    ("DELETE", "/schedules/1", None),
    ("POST", "/schedules/1/run", None),
    ("PUT", "/config", {}),
    ("PUT", "/config/smtp-password", {"password": "x"}),
    ("DELETE", "/config/smtp-password", None),
    ("POST", "/notifications/test-mail", None),
    ("POST", "/clamd/start", None),
    ("POST", "/clamd/stop", None),
    ("POST", "/diagnostics", None),
    ("DELETE", "/logs", None),
]


@pytest.mark.parametrize("method,path,body", _WRITE_CALLS)
def test_state_changes_need_the_admin_role(env, method, path, body):
    assert _call(env, method, path, body, headers={}).status_code == 401
    response = _call(env, method, path, body, headers=env.user)
    assert response.status_code == 403
    assert _error_code(response) == "E8102"
    assert env.changed == [] and env.realtime.calls == [] and env.mails == []


def test_state_change_from_another_site_is_rejected(env):
    # ブラウザで開いた別のサイトから、このPC の API を操作させない
    response = env.client.put(f"{API_PREFIX}/realtime", json={"enabled": True},
                              headers={**env.admin, "Origin": "https://evil.example"})
    assert response.status_code == 403
    assert env.realtime.calls == []


def test_write_apis_are_absent_without_an_invoker(env, tmp_path):
    env.context.invoker = None
    client = TestClient(create_app(ApiTokens(tmp_path / "t2"), env.context), base_url="http://127.0.0.1:8765",
                        client=("127.0.0.1", 50000))
    assert client.put(f"{API_PREFIX}/realtime", json={"enabled": True}).status_code in (401, 404, 405)


@pytest.mark.parametrize("body", [{"enabled": "true"}, {"enabled": 1}, {"enabled": True, "x": 1}, {}])
def test_bodies_are_checked_strictly(env, body):
    # 型の違う値（"true"・1）や余計な項目を受け付けない
    response = _call(env, "PUT", "/realtime", body)
    assert response.status_code == 400
    assert _error_code(response) == "E8100"
    assert env.realtime.calls == []


# -- 6.3 隔離 -----------------------------------------------------------------------------
def _quarantined(env, name="x.exe"):
    folder = env.tmp / "work"
    folder.mkdir(exist_ok=True)
    original = folder / name
    original.write_bytes(b"content")
    row = env.context.quarantine_service.quarantine(original, "Eicar")
    return row, original


def test_restore_goes_back_to_the_original_place_only(env):
    row, original = _quarantined(env)
    # 復元先を指定させない（SYSTEM・root の権限で任意の場所に書き込ませないため）
    other = env.tmp / "other.exe"
    response = _call(env, "POST", f"/quarantine/{row.id}/restore", {"dest_path": str(other)})
    assert response.status_code == 400
    assert not other.exists()

    assert _call(env, "POST", f"/quarantine/{row.id}/restore", {}).status_code == 204
    assert original.read_bytes() == b"content"
    assert env.changed == ["quarantine"]
    # 復元済みのものはもう一度復元・削除できない
    assert _call(env, "POST", f"/quarantine/{row.id}/restore", {}).status_code == 400
    assert _call(env, "DELETE", f"/quarantine/{row.id}").status_code == 400


def test_restore_does_not_overwrite_unless_asked(env):
    row, original = _quarantined(env)
    original.write_bytes(b"new file")
    response = _call(env, "POST", f"/quarantine/{row.id}/restore", {"overwrite": False})
    assert response.status_code == 400
    assert _error_code(response) == "E3002"
    assert original.read_bytes() == b"new file"
    assert _call(env, "POST", f"/quarantine/{row.id}/restore", {"overwrite": True}).status_code == 204
    assert original.read_bytes() == b"content"


def test_delete_quarantine(env):
    row, _ = _quarantined(env)
    assert _call(env, "DELETE", f"/quarantine/{row.id}").status_code == 204
    assert env.context.quarantine_service.get(row.id).deleted == 1
    response = _call(env, "DELETE", "/quarantine/999")
    assert response.status_code == 404 and _error_code(response) == "E3003"


# -- 6.4 除外 -----------------------------------------------------------------------------
def test_excludes_add_toggle_remove(env):
    env.realtime.is_running = True
    response = _call(env, "POST", "/excludes", {"type": "extension", "value": "LOG"})
    assert response.status_code == 201
    rule = response.json()
    assert rule == {"id": rule["id"], "type": "extension", "value": ".log", "enabled": True}  # 画面の入力と同じ形に直す
    assert _call(env, "PATCH", f"/excludes/{rule['id']}", {"enabled": False}).status_code == 204
    assert env.context.exclude_service.list_all()[0].enabled == 0
    assert _call(env, "DELETE", f"/excludes/{rule['id']}").status_code == 204
    assert env.context.exclude_service.list_all() == []
    # 監視中は変更を反映し、画面も読み直させる
    assert env.realtime.calls == [("excludes", "extension")] * 3
    assert env.changed == ["excludes"] * 3


@pytest.mark.parametrize("body", [
    {"type": "folder", "value": "relative/dir"},
    {"type": "folder", "value": "   "},
    {"type": "registry", "value": "x"},
])
def test_invalid_excludes_are_rejected(env, body):
    response = _call(env, "POST", "/excludes", body)
    assert response.status_code == 400
    assert env.context.exclude_service.list_all() == []


def test_duplicate_exclude_is_rejected(env):
    assert _call(env, "POST", "/excludes", {"type": "extension", "value": ".tmp"}).status_code == 201
    assert _call(env, "POST", "/excludes", {"type": "extension", "value": "tmp"}).status_code == 400


@pytest.mark.parametrize("method,path,body", [
    ("PATCH", "/excludes/42", {"enabled": True}),
    ("DELETE", "/excludes/42", None),
    ("PATCH", "/realtime/targets/42", {"enabled": True}),
    ("DELETE", "/realtime/targets/42", None),
    ("PATCH", "/schedules/42", {"enabled": True}),
    ("PUT", "/schedules/42", {"name": "a", "scan_type": "update", "cron": "0 3 * * *"}),
    ("DELETE", "/schedules/42", None),
    ("POST", "/schedules/42/run", None),
])
def test_unknown_ids_are_404(env, method, path, body):
    response = _call(env, method, path, body)
    assert response.status_code == 404
    assert _error_code(response) == "E8109"


# -- 6.5 リアルタイム監視 -------------------------------------------------------------------
def test_realtime_on_off(env):
    env.context.watch_target_service.add(str(env.tmp))
    assert _call(env, "PUT", "/realtime", {"enabled": True}).json() == {"running": True}
    assert _call(env, "PUT", "/realtime", {"enabled": False}).json() == {"running": False}
    assert env.realtime.calls == ["start", "stop"]
    assert env.changed == ["realtime", "realtime"]


def test_realtime_without_targets_is_400(env):
    env.realtime.can_start = False
    response = _call(env, "PUT", "/realtime", {"enabled": True})
    assert response.status_code == 400


def test_realtime_start_failure_is_500_without_details(env):
    env.context.watch_target_service.add(str(env.tmp))
    env.realtime.can_start = False
    response = _call(env, "PUT", "/realtime", {"enabled": True})
    assert response.status_code == 500
    assert response.json()["error"]["detail"] is None


def test_watch_targets(env):
    env.realtime.is_running = True
    folder = env.tmp / "watch"
    folder.mkdir()
    response = _call(env, "POST", "/realtime/targets", {"path": str(folder)})
    assert response.status_code == 201
    target = response.json()
    assert target["path"] == str(folder) and target["enabled"] is True
    assert _call(env, "POST", "/realtime/targets", {"path": str(folder)}).status_code == 400  # 重複
    assert _call(env, "PATCH", f"/realtime/targets/{target['id']}", {"enabled": False}).status_code == 204
    assert _call(env, "DELETE", f"/realtime/targets/{target['id']}").status_code == 204
    assert env.realtime.calls == [("add_target", str(folder)), ("remove_target", str(folder)),
                                  ("remove_target", str(folder))]


@pytest.mark.parametrize("path", ["relative", "/no/such/folder/for/clamqtpie"])
def test_watch_target_must_be_an_existing_absolute_folder(env, path):
    assert _call(env, "POST", "/realtime/targets", {"path": path}).status_code == 400


# -- 6.6 スケジュール -----------------------------------------------------------------------
def test_schedules_crud(env):
    body = {"name": "毎晩", "scan_type": "scan", "cron": "0 3 * * *", "target": str(env.tmp)}
    response = _call(env, "POST", "/schedules", body)
    assert response.status_code == 201
    job = response.json()
    assert job["name"] == "毎晩" and job["enabled"] is True
    response = _call(env, "PUT", f"/schedules/{job['id']}", {**body, "cron": "30 4 * * *"})
    assert response.json()["cron"] == "30 4 * * *"
    assert _call(env, "PATCH", f"/schedules/{job['id']}", {"enabled": False}).status_code == 204
    assert env.context.scheduler_service.list_all()[0].enabled is False
    assert _call(env, "DELETE", f"/schedules/{job['id']}").status_code == 204
    assert env.context.scheduler_service.list_all() == []
    assert env.changed == ["schedules"] * 4


def test_scan_schedule_target_must_be_absolute(env):
    response = _call(env, "POST", "/schedules", {"name": "a", "scan_type": "scan", "cron": "0 3 * * *",
                                                 "target": "relative/dir"})
    assert response.status_code == 400
    assert env.context.scheduler_service.list_all() == []


def test_invalid_cron_is_400(env):
    response = _call(env, "POST", "/schedules", {"name": "a", "scan_type": "update", "cron": "every day"})
    assert response.status_code == 400
    assert _error_code(response) == "E4002"


def test_run_schedule_now_and_refuse_while_running(env):
    job = env.context.scheduler_service.add_job("毎晩", "scan", "0 3 * * *", str(env.tmp))
    assert _call(env, "POST", f"/schedules/{job.id}/run").status_code == 202
    assert env.run_started.wait(5)
    # 同じジョブを実行中なら受け付けない
    response = _call(env, "POST", f"/schedules/{job.id}/run")
    assert response.status_code == 409
    env.release_run.set()
    assert env.scanned == [(str(env.tmp), "毎晩")]


# -- 6.8 設定 -------------------------------------------------------------------------------
def _config_body(env):
    body = _call(env, "GET", "/config", headers=env.user).json()
    body.pop("protected")
    return body


def test_get_config_hides_api_settings_and_password(env):
    env.passwords["smtp"] = "secret"
    body = _call(env, "GET", "/config", headers=env.user).json()
    assert "api" not in body
    assert body["smtp"]["has_password"] is True
    assert "secret" not in str(body)
    assert {"update.freshclam_path", "clamd.tcp_port"} <= set(body["protected"])
    # 利用者ごとの GUI の設定（gui.json）は、サービスの設定に含めない
    assert "general" not in body and "general.autostart" not in body["protected"]
    assert "realtime.default_action" not in body["protected"]


def test_put_config_changes_the_live_settings(env):
    live_realtime = env.context.config_service.current.realtime
    body = _config_body(env)
    body["realtime"]["default_action"] = "notify_only"
    body["update"]["stale_days"] = 7
    assert _call(env, "PUT", "/config", body).status_code == 204
    # 各サービスが持っている設定のオブジェクトにも反映される（設定画面と同じ）
    assert live_realtime.default_action == "notify_only"
    saved = ConfigService(env.tmp / "config.json").load()
    assert saved.update.stale_days == 7
    assert env.applied == [env.context.config_service.current]
    assert env.changed == ["config"]


@pytest.mark.parametrize("field", ["update.freshclam_path", "realtime.clamonacc_path", "clamd.tcp_host",
                                   "clamd.tcp_port", "clamd.service_name", "clamd.connection_type",
                                   "clamd.max_concurrent_connections"])
def test_protected_settings_cannot_be_changed(env, field):
    # 実行ファイルの場所・clamd の接続先・サービス名・画面で変えられない項目は、Web API からは変えられない
    body = _config_body(env)
    section, name = field.split(".")
    current = body[section][name]
    if isinstance(current, bool):
        body[section][name] = not current
    elif isinstance(current, int):
        body[section][name] = current + 1
    else:
        body[section][name] = "tcp" if name == "connection_type" else "/tmp/evil"
    response = _call(env, "PUT", "/config", body)
    assert response.status_code == 400
    assert field in response.json()["error"]["detail"]
    assert env.applied == []


def test_api_settings_are_not_accepted(env):
    body = _config_body(env)
    body["api"] = {"enabled": True, "port": 8765}
    assert _call(env, "PUT", "/config", body).status_code == 400


def test_gui_settings_are_not_accepted(env):
    body = _config_body(env)
    body["general"] = {"autostart": False}
    assert _call(env, "PUT", "/config", body).status_code == 400


@pytest.mark.parametrize("change,field", [
    (lambda b: b["update"].__setitem__("stale_days", 99), "update.stale_days"),
    (lambda b: b["update"].__setitem__("interval_hours", 0), "update.interval_hours"),
    (lambda b: b["update"].__setitem__("stale_days", "3"), "update.stale_days"),
    (lambda b: b["update"].__setitem__("auto_update", 1), "update.auto_update"),
    (lambda b: b["logging"].pop("level"), "logging.level"),
    (lambda b: b["logging"].__setitem__("extra", 1), "logging.extra"),
])
def test_invalid_config_is_rejected_with_the_field_name(env, change, field):
    body = _config_body(env)
    change(body)
    response = _call(env, "PUT", "/config", body)
    assert response.status_code == 400
    assert field in response.json()["error"]["detail"]
    assert ConfigService(env.tmp / "config.json").load().update.stale_days == 3


def test_changing_the_smtp_server_removes_the_saved_password(env):
    # 送信先を差し替えて、保存しているパスワードを送らせない
    env.passwords["smtp"] = "secret"
    body = _config_body(env)
    body["smtp"]["to_address"] = "a@example.com"
    assert _call(env, "PUT", "/config", body).status_code == 204
    assert env.passwords == {"smtp": "secret"}  # 送信先のサーバー以外の変更では消さない
    body["smtp"]["host"] = "smtp.example.net"
    assert _call(env, "PUT", "/config", body).status_code == 204
    assert env.passwords == {}


def test_smtp_password(env):
    assert _call(env, "PUT", "/config/smtp-password", {"password": "pw"}).status_code == 204
    assert env.passwords == {"smtp": "pw"}
    assert _call(env, "PUT", "/config/smtp-password", {"password": ""}).status_code == 400
    assert _call(env, "DELETE", "/config/smtp-password").status_code == 204
    assert env.passwords == {}


def test_test_mail(env):
    assert _call(env, "POST", "/notifications/test-mail").status_code == 204
    assert env.mails == [1]


# -- 6.1 clamd・診断 -----------------------------------------------------------------------
def test_clamd_start(env):
    assert _call(env, "POST", "/clamd/start").status_code == 202
    env.clamd_starts["ok"] = False  # 既に起動を試みている
    response = _call(env, "POST", "/clamd/start")
    assert response.status_code == 409


def test_clamd_stop_suppresses_auto_start(env):
    assert _call(env, "POST", "/clamd/stop").status_code == 204
    assert env.controller.stopped == 1
    assert env.stop_hooks == ["requested"]
    assert env.changed == ["clamd"]


def test_clamd_stop_failure_restores_auto_start(env):
    env.controller.fail = True
    response = _call(env, "POST", "/clamd/stop")
    assert response.status_code == 500
    assert env.stop_hooks == ["requested", "failed"]


def test_diagnostics_report_is_masked(env):
    body = _call(env, "POST", "/diagnostics").json()
    assert "/run/secret/clamd.ctl" not in body["report"]
    assert "<socket>" in body["report"]


# -- 6.9 ログ -------------------------------------------------------------------------------
def test_clear_logs(env):
    response = _call(env, "DELETE", "/logs")
    assert response.status_code == 200
    assert env.changed == ["logs"]


def test_changes_run_on_the_main_thread(env, qapp, tmp_path):
    # 実際の待ち受け（uvicorn のスレッド）から、状態を変える処理がメインスレッドで実行される
    import httpx2 as httpx

    from api.bridge import QtInvoker
    from api.server import LOOPBACK_HOST, ApiServer

    main_thread = threading.current_thread()
    seen = []
    env.context.invoker = QtInvoker()
    env.context.notify_changed = lambda what: seen.append((what, threading.current_thread() is main_thread))
    tokens = ApiTokens(tmp_path / "live_tokens")
    server = ApiServer(create_app(tokens, env.context), 0)
    server.start()
    result = {}

    def request():
        result["response"] = httpx.post(
            f"http://{LOOPBACK_HOST}:{server.port}{API_PREFIX}/excludes",
            json={"type": "extension", "value": ".bak"},
            headers={"Authorization": f"Bearer {tokens.token_for(ROLE_ADMIN)}"}, timeout=10)

    worker = threading.Thread(target=request)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while worker.is_alive() and time.monotonic() < deadline:
            qapp.processEvents()  # メインスレッドのイベントループを回して、渡された処理を実行させる
            worker.join(0.01)
    finally:
        server.stop()
    assert result["response"].status_code == 201
    assert seen == [("excludes", True)]


def test_changes_through_the_api_are_logged(env, monkeypatch):
    # Web API で変えた内容を後から追えるよう、ログに残す。設定は値を残さず、変えた項目の名前だけ
    events = []
    monkeypatch.setattr("api.actions.log_event", lambda category, message: events.append((category, message)))
    _call(env, "POST", "/excludes", {"type": "extension", "value": ".tmp"})
    body = _config_body(env)
    body["smtp"]["to_address"] = "someone@example.com"
    _call(env, "PUT", "/config", body)
    assert events == [
        ("リアルタイム", "Web API: 除外を追加しました: extension .tmp（有効）"),
        ("システム", "Web API: 設定を保存しました: smtp.to_address"),
    ]
