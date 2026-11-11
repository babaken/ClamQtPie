"""スケジュールのためにスリープから起こすタイマーのテスト（サービス化・Web API設計書 6.6節・9.1節。段階 12-3c）"""
from __future__ import annotations

import io
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core import wake_timer as wake_module
from core.scheduler_service import WAKE_LEAD, SchedulerService
from core.wake_timer import SystemdWakeTimer, WakeTimer, parse_rtcwake, wake_timer_policy
from db.database import DatabaseService


class FakeTimer(WakeTimer):
    def __init__(self):
        self.calls = []

    def set(self, when):
        self.calls.append(("set", when))

    def clear(self):
        self.calls.append(("clear", None))

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


@pytest.fixture
def make(tmp_path):
    db = DatabaseService(tmp_path / "app.db")
    db.connect()
    services = []

    def build(timer=None, start=True):
        timer = timer or FakeTimer()
        service = SchedulerService(db, lambda target, name=None: None, lambda: None, wake_timer=timer)
        services.append(service)
        if start:
            service.start()
        return service, timer

    yield build
    for service in services:
        service.shutdown()
    db.close()


def _next_fire(service, job_id):
    return service._scheduler.get_job(f"schedule_{job_id}").next_run_time


def test_wake_is_off_by_default(make):
    service, timer = make()
    # 起動時は、前回の異常終了で残ったタイマーがあれば消す
    assert timer.calls == [("clear", None)]
    record = service.add_job("毎日", "update", "0 3 * * *")
    assert record.wake is False
    assert timer.calls == [("clear", None)]  # 起こすジョブが無ければ設定しない


def test_timer_follows_the_earliest_wake_job(make):
    service, timer = make()
    late = service.add_job("遅い", "update", "0 23 * * *", wake=True)
    assert timer.last == ("set", _next_fire(service, late.id) - WAKE_LEAD)  # 実行の 2 分前に起こす
    early = service.add_job("早い", "update", "0 1 * * *", wake=True)
    earliest = min(_next_fire(service, late.id), _next_fire(service, early.id))
    assert timer.last == ("set", earliest - WAKE_LEAD)
    service.add_job("起こさない", "update", "* * * * *")  # wake の無いジョブは関係しない
    assert timer.last == ("set", earliest - WAKE_LEAD)


def test_timer_is_updated_when_jobs_change(make):
    service, timer = make()
    job = service.add_job("毎日", "update", "0 3 * * *", wake=True)
    service.set_enabled(job.id, False)
    assert timer.last == ("clear", None)
    service.set_enabled(job.id, True)
    assert timer.last[0] == "set"
    # 編集で wake を渡さなければ（従来のアプリの画面）そのまま、False なら消す
    service.update_job(job.id, "毎日", "update", "0 4 * * *")
    assert service.list_all()[0].wake is True and timer.last == ("set", _next_fire(service, job.id) - WAKE_LEAD)
    service.update_job(job.id, "毎日", "update", "0 4 * * *", wake=False)
    assert timer.last == ("clear", None)
    service.update_job(job.id, "毎日", "update", "0 4 * * *", wake=True)
    service.remove_job(job.id)
    assert timer.last == ("clear", None)


def test_same_time_is_not_set_again(make):
    service, timer = make()
    job = service.add_job("毎日", "update", "0 3 * * *", wake=True)
    count = len(timer.calls)
    service.refresh_wake()
    service.run_now(job.id)  # 実行の後も設定し直すが、時刻が同じなら OS に設定し直さない
    assert len(timer.calls) == count


def test_timer_is_set_on_start_and_cleared_on_shutdown(make):
    service, timer = make(start=False)
    service.add_job("毎日", "update", "0 3 * * *", wake=True)
    assert timer.calls == []
    service.start()
    assert timer.last[0] == "set"
    service.shutdown()  # サービスが止まっている間は起こさない
    assert timer.last == ("clear", None)


def test_wake_time_already_passed_is_not_set(make, monkeypatch):
    service, timer = make()
    job = service.add_job("すぐ", "update", "0 3 * * *", wake=True)
    soon = datetime.now(timezone.utc) + timedelta(seconds=30)  # 実行まで 2 分を切っている（もう起きている）
    monkeypatch.setattr(service, "next_wake_time", lambda: soon - WAKE_LEAD)
    service.refresh_wake()
    assert timer.last == ("clear", None)
    assert job.id


def test_timer_failure_does_not_break_the_schedule(make, caplog):
    class Broken(WakeTimer):
        def set(self, when):
            raise OSError("access denied")

    service, _ = make(timer=Broken())
    record = service.add_job("毎日", "update", "0 3 * * *", wake=True)
    assert record.id and "スリープから起こすタイマーを設定できませんでした" in caplog.text


def test_resume_refreshes_the_timer(make, monkeypatch):
    service, timer = make()
    calls = []
    monkeypatch.setattr(service, "refresh_wake", lambda: calls.append("refresh"))
    service.on_resume(600)
    assert calls == ["refresh"]


# -- systemd ---------------------------------------------------------------------------------
def test_systemd_timer_writes_units_and_reloads(tmp_path):
    commands = []
    timer = SystemdWakeTimer(unit_dir=tmp_path, run=lambda cmd, **kw: commands.append(cmd[1:]))
    timer._systemctl = "/usr/bin/systemctl"
    when = datetime(2026, 11, 3, 2, 58, tzinfo=timezone.utc)
    timer.set(when)
    text = (tmp_path / "clamqtpie-wake.timer").read_text(encoding="utf-8")
    assert "OnCalendar=2026-11-03 02:58:00 UTC" in text and "WakeSystem=true" in text
    assert "ExecStart=" in (tmp_path / "clamqtpie-wake.service").read_text(encoding="utf-8")
    assert commands == [["daemon-reload"], ["restart", "clamqtpie-wake.timer"]]
    commands.clear()
    timer.clear()
    assert not list(tmp_path.iterdir())
    assert commands == [["stop", "clamqtpie-wake.timer"], ["daemon-reload"]]
    commands.clear()
    timer.clear()  # 無ければ何もしない
    assert commands == []


def test_systemd_timer_without_systemctl_fails_softly(tmp_path):
    timer = SystemdWakeTimer(unit_dir=tmp_path, run=lambda *a, **k: None)
    timer._systemctl = None
    with pytest.raises(OSError):
        timer.set(datetime.now(timezone.utc))


def test_non_root_linux_gets_a_timer_that_does_nothing(monkeypatch):
    monkeypatch.setattr(wake_module.os, "geteuid", lambda: 1000, raising=False)
    assert type(wake_module.create_wake_timer("Linux")) is WakeTimer


@pytest.mark.skipif(sys.platform != "win32", reason="Windows の API")
def test_windows_timer_can_be_set_and_cancelled():
    timer = wake_module.WindowsWakeTimer()
    timer.set(datetime.now(timezone.utc) + timedelta(days=1))
    timer.clear()


# -- 電源の設定 ------------------------------------------------------------------------------
_POWERCFG_JA = """電源設定の GUID: bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d  (スリープ解除タイマーの許可)
      利用可能な設定のインデックス: 000
      利用可能な設定のフレンドリ名: 無効
      利用可能な設定のインデックス: 001
      利用可能な設定のフレンドリ名: 有効
    現在の AC 電源設定のインデックス: 0x00000001
    現在の DC 電源設定のインデックス: 0x00000000
"""


def test_rtcwake_is_read_regardless_of_language():
    assert parse_rtcwake(_POWERCFG_JA) == (1, 0)
    assert parse_rtcwake(_POWERCFG_JA.replace("現在の AC 電源設定のインデックス", "Current AC Power Setting Index")) == (1, 0)
    assert parse_rtcwake("no values") is None


def test_wake_policy_on_windows(monkeypatch):
    monkeypatch.setattr(wake_module, "find_system_tool", lambda name, system=None: "C:/Windows/System32/powercfg.exe")
    text = wake_timer_policy("Windows", run=lambda *a, **k: SimpleNamespace(stdout=_POWERCFG_JA))
    assert "電源接続時 有効" in text and "バッテリー駆動時 無効" in text and "起こせません" in text

    def failing(*a, **k):
        raise subprocess.TimeoutExpired("powercfg", 10)

    assert "確認できません" in wake_timer_policy("Windows", run=failing)


def test_ctl_status_shows_the_wake_policy(tmp_path, monkeypatch):
    import clamqtpie_ctl

    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: False)
    monkeypatch.setattr("core.wake_timer.wake_timer_policy", lambda: "スリープ解除タイマー: テスト")
    out = io.StringIO()
    assert clamqtpie_ctl.main(["--data-dir", str(tmp_path), "status"], out=out) == 0
    assert "スリープ解除タイマー: テスト" in out.getvalue()
