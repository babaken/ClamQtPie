"""スケジュールの取りこぼし対策・スリープからの復帰・実行中のスリープの抑止のテスト（段階 12-3c）"""
from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from core import scheduler_service as scheduler_module
from core import sleep_inhibit
from core.config_model import ConfigModel, validate_and_fix
from core.resume_watcher import RESUME_THRESHOLD_SEC, ResumeWatcher
from core.scheduler_service import CATCH_UP_DELAY, SchedulerService
from db.database import DatabaseService
from db.models import Schedule


@pytest.fixture
def make_scheduler(tmp_path):
    db = DatabaseService(tmp_path / "app.db")
    db.connect()
    services = []

    def make(**kwargs):
        calls = []
        service = SchedulerService(db, lambda target, name=None: calls.append(("scan", target)),
                                   lambda: calls.append(("update", None)), **kwargs)
        service.calls = calls
        services.append(service)
        return service

    yield make, db
    for service in services:
        service.shutdown()
    db.close()


def _set_last_run(db, job_id, value):
    with db.session() as session:
        session.get(Schedule, job_id).last_run = value
        session.commit()


def test_scheduler_coalesces_and_allows_late_runs(make_scheduler):
    make, _ = make_scheduler
    service = make(misfire_grace_sec=1800)
    assert service._scheduler._job_defaults["coalesce"] is True
    assert service._scheduler._job_defaults["misfire_grace_time"] == 1800


def test_missed_run_is_caught_up_once(make_scheduler):
    make, db = make_scheduler
    service = make()
    service.start()
    record = service.add_job("毎日", "scan", "0 3 * * *", target="/data")
    now = datetime.now(timezone.utc)
    _set_last_run(db, record.id, (now - timedelta(days=2)).isoformat() + " (検知あり 1件)")

    assert service.catch_up_missed(now) == ["毎日"]
    job = service._scheduler.get_job(f"catch_up_{record.id}")
    assert job is not None and job.next_run_time == now + CATCH_UP_DELAY
    # 何度確かめても、取り返しは1回だけ登録する
    service.catch_up_missed(now)
    assert len([j for j in service._scheduler.get_jobs() if j.id.startswith("catch_up_")]) == 1


@pytest.mark.parametrize("last_run,enabled", [
    (None, True),                       # 一度も実行していない
    ("recent", True),                   # 過ぎた回が無い
    ("old", False),                     # 無効
    ("not a date", True),               # 読めない
])
def test_jobs_without_missed_runs_are_not_caught_up(make_scheduler, last_run, enabled):
    make, db = make_scheduler
    service = make()
    service.start()
    record = service.add_job("毎日", "scan", "0 3 * * *")
    now = datetime.now(timezone.utc)
    value = {"recent": (now - timedelta(minutes=1)).isoformat(), "old": (now - timedelta(days=2)).isoformat()}
    _set_last_run(db, record.id, value.get(last_run, last_run))
    if not enabled:
        service.set_enabled(record.id, False)
    assert service.catch_up_missed(now) == []


def test_resume_recalculates_the_schedule(make_scheduler, monkeypatch):
    make, db = make_scheduler
    service = make()
    service.start()
    record = service.add_job("毎日", "scan", "0 3 * * *")
    _set_last_run(db, record.id, (datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
    names = service.catch_up_missed
    calls = []
    monkeypatch.setattr(service, "catch_up_missed", lambda: calls.append("catch_up") or names())
    monkeypatch.setattr(service._scheduler, "wakeup", lambda: calls.append("wakeup"))
    assert service.on_resume(3600) == ["毎日"]
    assert calls[0] == "catch_up" and calls[-1] == "wakeup"  # 取り返しを登録してから、待ち時間を計算し直す


def test_finished_job_is_reported(make_scheduler):
    make, _ = make_scheduler
    service = make()
    finished = []
    service.on_job_finished = lambda *args: finished.append(args)
    record = service.add_job("更新", "update", "0 3 * * *")
    service.run_now(record.id)
    assert finished == [(record.id, "更新", "success", "")]


def test_jobs_keep_the_pc_awake_while_running(make_scheduler, monkeypatch):
    make, _ = make_scheduler
    events = []

    @contextmanager
    def fake_prevent(reason):
        events.append(("on", reason))
        yield
        events.append(("off", reason))

    monkeypatch.setattr(scheduler_module, "prevent_sleep", fake_prevent)
    service = make(inhibit_sleep=True)
    service.calls = []
    record = service.add_job("毎日", "scan", "0 3 * * *", target="/data")
    service.run_now(record.id)
    assert [e[0] for e in events] == ["on", "off"] and "毎日" in events[0][1]
    # 定義の自動更新も同じ
    events.clear()
    service.start()
    service.set_auto_update(lambda: events.append(("run", "")), True, 4)
    service._scheduler.get_job("auto_update").func()
    assert [e[0] for e in events] == ["on", "run", "off"]


def test_jobs_do_not_touch_sleep_by_default(make_scheduler, monkeypatch):
    make, _ = make_scheduler
    monkeypatch.setattr(scheduler_module, "prevent_sleep", lambda reason: pytest.fail("呼ばれないこと"))
    service = make()
    record = service.add_job("毎日", "scan", "0 3 * * *")
    service.run_now(record.id)


# -- スリープの抑止 ----------------------------------------------------------------------------
def test_linux_sleep_is_blocked_while_the_inhibitor_runs(monkeypatch):
    started = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            started.append(command)
            self.stdin = self
            self.closed = False

        def close(self):
            self.closed = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(sleep_inhibit, "find_system_tool", lambda name, system=None: f"/usr/bin/{name}")
    monkeypatch.setattr(sleep_inhibit.subprocess, "Popen", FakeProcess)
    with sleep_inhibit.prevent_sleep("スキャン中", system="Linux"):
        assert started[0][:2] == ["/usr/bin/systemd-inhibit", "--what=sleep"]
        assert started[0][-1] == "/usr/bin/cat" and "--why=スキャン中" in started[0]
    assert started  # 終わったら標準入力を閉じて、抑止のプロセスを終わらせる


def test_sleep_inhibit_failure_does_not_stop_the_job(monkeypatch):
    def failing(*args, **kwargs):
        raise OSError("not found")

    monkeypatch.setattr(sleep_inhibit, "find_system_tool", lambda name, system=None: f"/usr/bin/{name}")
    monkeypatch.setattr(sleep_inhibit.subprocess, "Popen", failing)
    ran = []
    with sleep_inhibit.prevent_sleep("x", system="Linux"):
        ran.append(True)
    assert ran == [True]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows の API")
def test_windows_sleep_is_blocked_for_the_running_thread():
    with sleep_inhibit.prevent_sleep("x", system="Windows"):
        pass


# -- 復帰の検出 ------------------------------------------------------------------------------
def test_resume_is_detected_from_a_clock_jump(qapp):
    now = [1000.0]
    watcher = ResumeWatcher(clock=lambda: now[0], interval_sec=30)
    watcher.start()
    resumed = []
    watcher.resumed.connect(resumed.append)
    now[0] += 30
    watcher.check()
    assert resumed == []
    now[0] += 30 + RESUME_THRESHOLD_SEC + 10
    watcher.check()
    assert resumed == [RESUME_THRESHOLD_SEC + 10]
    now[0] -= 600  # 時計が戻っても復帰とはみなさない
    watcher.check()
    assert len(resumed) == 1
    watcher.stop()


# -- 設定 ------------------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [(600, 600), (10, 3600), (100000, 3600), ("1", 3600), (True, 3600)])
def test_misfire_grace_is_validated(value, expected):
    model = ConfigModel.from_dict({"schedule": {"misfire_grace_sec": value}})
    validate_and_fix(model, lambda _message: None)
    assert model.schedule.misfire_grace_sec == expected


def test_schedule_finished_event(qapp):
    from api.bridge import EventHub
    from api.events import connect_scheduler

    hub = EventHub()
    target = type("S", (), {"on_job_finished": None})()
    connect_scheduler(hub, target)
    target.on_job_finished(3, "毎日\x1b[31m", "failure", "失敗")
    event = hub._history[-1]
    assert event.name == "schedule.finished"
    assert event.data == {"job_id": 3, "name": "毎日\\x1b[31m", "result": "failure", "note": "失敗"}
