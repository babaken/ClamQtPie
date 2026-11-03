"""トレイGUI のプロキシ（api_client/proxies.py）とトレイGUI の起動（tray_main.py）のテスト（段階 12-4b）

本物の FastAPI のアプリ（DB・隔離・除外・スケジュール・設定・ログは本物のサービス）を uvicorn で待ち受けさせ、
プロキシからつなぐ。admin の操作は、admin のトークンを user.token として置いた別のフォルダで確かめる
（段階 12-4c までは、トレイGUI の user のトークンでは 403 になる）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import shutil
import threading
from datetime import date
from types import SimpleNamespace

import pytest

from api.app import create_app
from api.auth import ROLE_ADMIN, ApiTokens
from api.bridge import EventHub
from api.context import ApiContext
from api.jobs import JobTracker
from api.server import ApiServer
from api_client.client import ApiClient
from api_client.events import EventStream
from api_client.proxies import ServiceProxies
from api_client.sync import SyncCaller
from core.config_service import ConfigService
from core.exceptions import ApiForbiddenError
from core.exclude_service import ExcludeService
from core.gui_config import read_gui_general
from core.log_reader_service import LogReaderService
from core.quarantine_service import QuarantineService
from core.scheduler_service import SchedulerService
from core.service_endpoint import write_endpoint
from core.watch_target_service import WatchTargetService
from db.database import DatabaseService
from tests.conftest import FAKE_INFECTED_CONTENT


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


class _Realtime:
    mode_name = "watchdog監視"

    def __init__(self):
        self.is_running = False

    def start(self):
        self.is_running = True
        return True

    def stop(self):
        self.is_running = False

    def add_target(self, path):
        pass

    def remove_target(self, path):
        pass

    def on_excludes_changed(self, type_):
        pass


class _ScanDispatcher:
    def __init__(self, jobs):
        self.jobs, self.is_running, self.started = jobs, False, []

    def start(self, targets, include_subfolders=True, apply_exclude=True, label="", skip_dirs=(), **kwargs):
        self.started.append(targets)
        self.is_running = True
        self.jobs.on_scan_started(label)

    def finish(self):
        self.is_running = False
        self.jobs.on_scan_finished({"scanned": 3, "detected": 1, "errors": 0, "cancelled": False})


@pytest.fixture
def service(tmp_path):
    db = DatabaseService(tmp_path / "app.db")
    db.connect()
    config_service = ConfigService(tmp_path / "config.json", gui_settings=False)
    config_service.load()
    log = tmp_path / "logs" / "app.log"
    log.parent.mkdir()
    log.write_text(f"{date.today()} 10:00:00,000 [INFO] <スキャン> 手動スキャンを開始しました\n", encoding="utf-8")
    hub = EventHub()
    jobs = JobTracker(hub)
    scan = _ScanDispatcher(jobs)
    scheduler = SchedulerService(db, lambda target, name=None: SimpleNamespace(detected=0),
                                 lambda: SimpleNamespace(result="success"))
    context = ApiContext(
        config_service=config_service,
        clamav_service=SimpleNamespace(ping=lambda retry=True: True,
                                       get_version=lambda: ("ClamAV 1.5.4", "28170", "Fri Oct 30 08:00:00 2026")),
        realtime_manager=_Realtime(), scan_dispatcher=scan,
        update_service=SimpleNamespace(last_executed_at=lambda: None, list_history=lambda: []),
        update_dispatcher=SimpleNamespace(is_running=False), scheduler_service=scheduler,
        quarantine_service=QuarantineService(db, tmp_path / "quarantine"),
        exclude_service=ExcludeService(db), watch_target_service=WatchTargetService(db),
        log_reader_service=LogReaderService(log), invoker=_DirectInvoker(), events=hub, jobs=jobs,
        apply_config=lambda saved: hub.publish("config.changed", {}),
    )
    api_dir = tmp_path / "api"
    tokens = ApiTokens(api_dir)
    server = ApiServer(create_app(tokens, context), 0, tokens=tokens)
    tokens.write_files()
    server.start()
    write_endpoint(api_dir, server.port)
    # admin の操作を確かめるため、admin のトークンを user.token として置いたフォルダ
    admin_dir = tmp_path / "api_admin"
    shutil.copytree(api_dir, admin_dir)
    (admin_dir / "user.token").write_text(tokens.token_for(ROLE_ADMIN), encoding="ascii")
    yield SimpleNamespace(api_dir=api_dir, admin_dir=admin_dir, hub=hub, scan=scan, context=context, tmp=tmp_path,
                          scheduler=scheduler)
    server.stop()
    db.close()


def _proxies(service, admin=False):
    client = ApiClient(service.admin_dir if admin else service.api_dir, verify=lambda port: None)
    client.prepare()
    return ServiceProxies(SyncCaller(client), EventStream(client, reconnect_ms=200),
                          gui_path=service.tmp / "gui.json")


def test_reads_go_through_the_api(qapp, service):
    p = _proxies(service)
    assert p.clamav.ping() is True and p.clamav.get_version()[0] == "ClamAV 1.5.4"
    assert p.realtime.is_running is False and p.realtime.mode_name == "watchdog監視"
    assert p.quarantine.list_all() == [] and p.excludes.list_all() == [] and p.scheduler.list_all() == []
    entries = p.log_reader.read_entries()
    assert [e.message for e in entries] == ["手動スキャンを開始しました"] and entries[0].category == "スキャン"
    assert p.log_reader.filter_entries(entries, keyword="開始") == entries  # 絞り込みは手元で行う
    assert p.config.current.update.stale_days == 3


def test_admin_operations_need_admin(qapp, service):
    p = _proxies(service)
    with pytest.raises(ApiForbiddenError):
        p.excludes.add("extension", ".tmp")  # 12-4c で昇格ヘルパーを通すまでは 403


def test_admin_operations_through_the_proxies(qapp, service):
    p = _proxies(service, admin=True)
    item = p.excludes.add("extension", ".tmp")
    p.excludes.set_enabled(item.id, False)
    assert [(e.value, e.enabled) for e in p.excludes.list_all()] == [(".tmp", False)]
    target = p.watch_targets.add(str(service.tmp))
    assert p.watch_targets.list_enabled_paths() == [str(service.tmp)]
    p.watch_targets.remove(target.id)
    job = p.scheduler.add_job("毎日", "update", "0 3 * * *", wake=True)
    p.scheduler.update_job(job.id, "毎日", "update", "0 4 * * *")  # 編集画面に wake が無くても保つ
    assert [(r.cron, r.wake) for r in p.scheduler.list_all()] == [("0 4 * * *", True)]
    assert p.realtime.start() is True and p.realtime.is_running is True


def test_manual_quarantine_and_restore(qapp, service):
    victim = service.tmp / "eicar.txt"
    victim.write_text(FAKE_INFECTED_CONTENT)
    p = _proxies(service, admin=True)
    row = p.quarantine.quarantine(victim, "Eicar")
    assert not victim.exists()
    [listed] = p.quarantine.list_all()
    assert listed.id == row.id and listed.restored == 0 and listed.deleted == 0
    p.quarantine.restore(row.id)
    assert victim.exists() and p.quarantine.list_all()[0].restored == 1


def test_gui_settings_are_kept_per_user(qapp, service):
    p = _proxies(service)  # user のトークンでも、GUI の設定は保存できる
    config = p.config.current
    config.general.start_minimized = True
    p.config.save(config)  # サービスの設定は変えていないので PUT /config は送らない
    assert read_gui_general(service.tmp / "gui.json").start_minimized is True
    config.update.stale_days = 9
    with pytest.raises(ApiForbiddenError):
        p.config.save(config)  # サービスの設定を変えたときだけ送る（admin）


def test_scan_progress_arrives_as_signals(qtbot, service):
    p = _proxies(service)
    with qtbot.waitSignal(p.events.connected, timeout=10000):
        p.events.start()
    target = str(service.tmp)
    with qtbot.waitSignal(p.scan.started, timeout=10000):
        p.scan.start([target], label="カスタムスキャン", scan_type="custom")
    assert service.scan.started == [[target]] and p.scan.is_running
    with qtbot.waitSignal(p.scan.finished, timeout=10000) as blocker:
        service.scan.finish()
    assert blocker.args[0]["detected"] == 1 and not p.scan.is_running
    p.events.stop()


def test_realtime_detection_event_becomes_a_signal(qtbot, service):
    p = _proxies(service)
    with qtbot.waitSignal(p.events.connected, timeout=10000):
        p.events.start()
    with qtbot.waitSignal(p.realtime.detected, timeout=10000) as blocker:
        service.hub.publish("realtime.detected", {"path": "/tmp/x", "virus_name": "Eicar"})
    assert blocker.args == ["/tmp/x", "Eicar"]
    p.events.stop()


def test_calls_from_a_background_thread(qtbot, service):
    p = _proxies(service)
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("q", p.quarantine.list_all()))
    thread.start()
    qtbot.waitUntil(lambda: "q" in result, timeout=10000)  # 画面のスレッドに通信を頼んで待つ
    thread.join()
    assert result["q"] == []


# -- トレイGUI の起動 ------------------------------------------------------------------------
def test_tray_builds_all_screens_against_the_service(qtbot, service, monkeypatch):
    import tray_main

    monkeypatch.setattr("api_client.client.verify_service_listener", lambda port: None)
    monkeypatch.setattr(tray_main.MainWindow, "show", lambda self: None)
    paths = SimpleNamespace(api_dir=service.api_dir)
    tray = tray_main.TrayApp(qtbot_app(qtbot), paths)
    tray.client._verify = lambda port: None
    assert tray.try_connect() is True
    assert tray.window is not None
    tray.window.get_screen("quarantine").reload()
    tray.window.get_screen("home").refresh_status()
    assert tray.window.get_screen("home").status.clamd_running is True
    tray.shutdown()  # 後のテストで、止めたサービスに画面が問い合わせないように


def test_tray_waits_for_the_service(qtbot, tmp_path):
    import tray_main

    tray = tray_main.TrayApp(qtbot_app(qtbot), SimpleNamespace(api_dir=tmp_path))
    assert tray.try_connect() is False  # サービスが動いていない（トークンが無い）
    assert tray.window is None and tray._reconnect.isActive()
    tray.shutdown()


def qtbot_app(qtbot):
    from PySide6.QtWidgets import QApplication

    return QApplication.instance()
