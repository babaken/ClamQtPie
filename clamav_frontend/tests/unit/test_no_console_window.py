"""Windows でコンソールのコマンドを起動するときに、コンソールのウィンドウを出さないこと

ウィンドウを持たないプロセス（トレイGUI・インストーラから起動したもの）がコンソールのコマンドをそのまま起動すると、
コンソール（Windows 11 の既定では Windows Terminal）のウィンドウが一瞬見える。
"""
from __future__ import annotations

import subprocess

import pytest

from core.exec_path import no_window_kwargs

#: CREATE_NO_WINDOW（subprocess に定数が無い OS でも値で確かめる）
CREATE_NO_WINDOW = 0x08000000


class _Recorder:
    """subprocess.run の代わり。受け取った引数を覚える"""

    def __init__(self, stdout=""):
        self.calls = []
        self.stdout = stdout

    def __call__(self, command, **kwargs):
        self.calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, self.stdout, "")

    @property
    def hidden(self) -> bool:
        return bool(self.calls) and all(c.get("creationflags", 0) & CREATE_NO_WINDOW for c in self.calls)


@pytest.fixture(autouse=True)
def _create_no_window_constant(monkeypatch):
    # Linux で動かすテストでも、Windows の値で確かめる
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW, raising=False)


def test_the_flag_is_only_for_windows():
    assert no_window_kwargs("Windows") == {"creationflags": CREATE_NO_WINDOW}
    assert no_window_kwargs("Linux") == {}


def test_desktop_toast_powershell_has_no_window(monkeypatch):
    """トレイGUI が通知（Toast）を出す・消すときの PowerShell"""
    import core.notification_service as notification_service

    run = _Recorder()
    monkeypatch.setattr(notification_service.subprocess, "run", run)
    notification_service._run_powershell("Write-Output 1", {})
    assert run.hidden


def test_group_membership_check_has_no_window(monkeypatch):
    """トレイGUI がサインインし直しを判定するときの net localgroup"""
    import core.service_access as service_access

    monkeypatch.setattr(service_access, "_IS_WINDOWS", True)
    run = _Recorder()
    service_access._run(["net", "localgroup"], runner=run)
    assert run.hidden


def test_service_setup_commands_have_no_window():
    """インストーラから起動する clamqtpie-ctl setup の sc・icacls など"""
    import core.windows_setup as windows_setup

    run = _Recorder()
    windows_setup._run(["sc", "query"], runner=run)
    assert run.hidden


def test_service_state_checks_have_no_window(monkeypatch):
    import core.service_status as service_status

    monkeypatch.setattr(service_status, "find_system_tool", lambda name, system=None: f"C:/Windows/System32/{name}.exe")
    run = _Recorder(stdout=b"STATE : 4  RUNNING")
    service_status.service_running("Windows", run=run)
    service_status.service_stopped("Windows", run=run)
    assert len(run.calls) == 2 and run.hidden


def test_wake_timer_check_has_no_window(monkeypatch):
    """トレイGUI の診断の powercfg"""
    import core.wake_timer as wake_timer

    monkeypatch.setattr(wake_timer, "find_system_tool", lambda name, system=None: f"C:/Windows/System32/{name}.exe")
    run = _Recorder()
    wake_timer.wake_timer_policy("Windows", run=run)
    assert run.hidden


def test_ctl_service_control_has_no_window(monkeypatch):
    import clamqtpie_ctl

    run = _Recorder()
    monkeypatch.setattr(clamqtpie_ctl.platform, "system", lambda: "Windows")
    monkeypatch.setattr(clamqtpie_ctl, "find_system_tool", lambda name, system=None: f"C:/Windows/System32/{name}.exe")
    monkeypatch.setattr(clamqtpie_ctl.subprocess, "run", run)
    clamqtpie_ctl._control_service("start")
    assert run.hidden


def test_freshclam_on_windows_has_no_window(monkeypatch, tmp_path):
    from core.update_service import UpdateService

    started = []

    class _Process:
        stdout = iter(())

        def wait(self):
            return 1

    def popen(command, **kwargs):
        started.append(kwargs)
        return _Process()

    freshclam = tmp_path / "freshclam.exe"
    freshclam.write_bytes(b"")
    monkeypatch.setattr("core.update_service.subprocess.Popen", popen)
    db = type("Db", (), {})()
    service = UpdateService(db, clamav_service=None, freshclam_path=str(freshclam), system="Windows",
                            validate_exec=lambda path, names, system=None: path)
    monkeypatch.setattr(service, "_safe_get_db_version", lambda: "1")
    monkeypatch.setattr(service, "_fail", lambda before, detail: detail)
    service.update_now()
    assert started and started[0]["creationflags"] & CREATE_NO_WINDOW
