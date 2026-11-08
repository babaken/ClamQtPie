"""OS の定義更新サービス（Linux の clamav-freshclam）の状態の表示と開始・停止のテスト"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from api_client.admin_ops import Operation, describe, is_admin
from core.exceptions import DefinitionUpdateError
from core.freshclam_service import (
    FRESHCLAM_UNIT,
    FreshclamServiceController,
    FreshclamServiceState,
    summary_text,
)
from tests.unit.test_api_write import _call, env  # noqa: F401 - pytest のフィクスチャ


class _Systemctl:
    """systemctl の代わり。show の応答と、enable・disable の結果を覚える"""

    def __init__(self, load="loaded", active="active", unit_file="enabled", fail=False):
        self.load, self.active, self.unit_file, self.fail = load, active, unit_file, fail
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if "show" in command:
            out = f"LoadState={self.load}\nActiveState={self.active}\nUnitFileState={self.unit_file}\n"
            return subprocess.CompletedProcess(command, 0, out, "")
        if self.fail:
            return subprocess.CompletedProcess(command, 1, "", "Failed to disable unit")
        running = "enable" in command
        self.active, self.unit_file = ("active", "enabled") if running else ("inactive", "disabled")
        return subprocess.CompletedProcess(command, 0, "", "")


def _controller(systemctl, system="Linux", tools=("systemctl", "pkexec")):
    return FreshclamServiceController(system=system, runner=systemctl,
                                      find_tool=lambda name, system: f"/usr/bin/{name}" if name in tools else None)


def test_status_reads_running_and_boot_start():
    state = _controller(_Systemctl()).status()
    assert state == FreshclamServiceState(available=True, active=True, enabled=True)
    state = _controller(_Systemctl(active="inactive", unit_file="disabled")).status()
    assert state == FreshclamServiceState(available=True, active=False, enabled=False)


@pytest.mark.parametrize("kwargs", [
    {"system": "Windows"},  # Windows には無い
    {"tools": ()},  # systemd が無い
])
def test_status_is_unavailable_without_systemd(kwargs):
    systemctl = _Systemctl()
    assert _controller(systemctl, **kwargs).status().available is False
    assert systemctl.commands == []


def test_status_is_unavailable_when_the_unit_is_not_installed():
    assert _controller(_Systemctl(load="not-found")).status().available is False


@pytest.mark.parametrize("running,verb", [(False, "disable"), (True, "enable")])
def test_stop_and_start_change_the_boot_start_too(monkeypatch, running, verb):
    """停止は今止めて PC の起動時にも起動しない（disable --now）、開始は今動かして PC の起動時にも起動する（enable --now）"""
    monkeypatch.setattr("core.freshclam_service.os.geteuid", lambda: 0, raising=False)
    systemctl = _Systemctl(active="inactive" if running else "active", unit_file="disabled" if running else "enabled")
    state = _controller(systemctl).set_running(running)
    assert ["/usr/bin/systemctl", verb, "--now", FRESHCLAM_UNIT] in systemctl.commands
    assert state.active is running and state.enabled is running


def test_ordinary_user_is_asked_to_authenticate(monkeypatch):
    """利用者の権限で動くとき（従来のアプリ）は pkexec を決まった場所から使う"""
    monkeypatch.setattr("core.freshclam_service.os.geteuid", lambda: 1000, raising=False)
    systemctl = _Systemctl()
    _controller(systemctl).set_running(False)
    assert ["/usr/bin/pkexec", "/usr/bin/systemctl", "disable", "--now", FRESHCLAM_UNIT] in systemctl.commands
    with pytest.raises(DefinitionUpdateError):
        _controller(_Systemctl(), tools=("systemctl",)).set_running(False)


def test_failure_is_reported(monkeypatch):
    monkeypatch.setattr("core.freshclam_service.os.geteuid", lambda: 0, raising=False)
    with pytest.raises(DefinitionUpdateError) as caught:
        _controller(_Systemctl(fail=True)).set_running(False)
    assert "Failed to disable unit" in caught.value.detail
    with pytest.raises(DefinitionUpdateError):
        _controller(_Systemctl(load="not-found")).set_running(True)


def test_summary_says_who_updates_the_definitions():
    running = FreshclamServiceState(True, True, True)
    stopped = FreshclamServiceState(True, False, False)
    assert "OS のサービス" in summary_text(running, auto_update=True)
    assert summary_text(stopped, auto_update=True) == "ClamQtPie が自動更新します"
    assert summary_text(FreshclamServiceState(), auto_update=True) == "ClamQtPie が自動更新します"
    assert "OFF" in summary_text(stopped, auto_update=False)


# -- API ------------------------------------------------------------------------------------------
class _FakeFreshclam:
    def __init__(self):
        self.state = FreshclamServiceState(True, True, True)
        self.calls = []

    def status(self):
        return self.state

    def set_running(self, running):
        self.calls.append(running)
        self.state = FreshclamServiceState(True, running, running)
        return self.state


def test_api_reads_the_state(env):  # noqa: F811
    body = _call(env, "GET", "/updates/freshclam-service", headers=env.user).json()
    assert body["available"] is False  # 無い（Windows など）
    env.context.freshclam_service = _FakeFreshclam()
    body = _call(env, "GET", "/updates/freshclam-service", headers=env.user).json()
    assert body["available"] and body["active"] and body["enabled"] and "OS のサービス" in body["summary"]


def test_api_start_and_stop_need_the_admin_role(env):  # noqa: F811
    fake = env.context.freshclam_service = _FakeFreshclam()
    response = _call(env, "PUT", "/updates/freshclam-service", {"enabled": False}, headers=env.user)
    assert response.status_code == 403 and fake.calls == []
    body = _call(env, "PUT", "/updates/freshclam-service", {"enabled": False}).json()
    assert fake.calls == [False] and body["active"] is False and body["summary"] == "ClamQtPie が自動更新します"
    assert env.changed == ["freshclam"]
    assert _call(env, "PUT", "/updates/freshclam-service", {"enabled": "no"}).status_code == 400


def test_api_without_the_service_is_not_found(env):  # noqa: F811
    assert _call(env, "PUT", "/updates/freshclam-service", {"enabled": True}).status_code == 404


def test_tray_asks_for_elevation_and_explains_the_change():
    assert is_admin("PUT", "/updates/freshclam-service")
    stop = describe(Operation("PUT", "/updates/freshclam-service", {"enabled": False}))
    start = describe(Operation("PUT", "/updates/freshclam-service", {"enabled": True}))
    assert "停止" in stop[0] and "ClamQtPie の自動更新" in stop[1]
    assert "開始" in start[0] and "起動時" in start[1]


# -- 定義更新画面（トレイGUI） ------------------------------------------------------------------------
def _screen(qtbot, freshclam):
    from gui.screens.update import UpdateScreen

    screen = UpdateScreen()
    qtbot.addWidget(screen)
    update_service = MagicMock()
    update_service.list_history.return_value = []
    clamav = MagicMock()
    clamav.get_version.return_value = ("ClamAV 1.4", "27200", "2026-11-08")
    screen.bind_services(update_service, MagicMock(), clamav, freshclam_service=freshclam)
    return screen


def test_update_screen_hides_the_card_without_the_service(qtbot):
    screen = _screen(qtbot, None)
    assert screen.freshclam_card.isHidden()
    screen.apply_freshclam_state({"available": False})
    assert screen.freshclam_card.isHidden()


def test_update_screen_shows_the_state_and_stops_the_service(qtbot):
    fake = MagicMock()
    fake.status.return_value = {"available": True, "active": True, "enabled": True, "summary": "OS のサービスが更新"}
    fake.set_running.return_value = {"available": True, "active": False, "enabled": False,
                                     "summary": "ClamQtPie が自動更新します"}
    screen = _screen(qtbot, fake)
    screen.reload_freshclam_service()
    qtbot.waitUntil(lambda: not screen.freshclam_card.isHidden())
    assert screen.freshclam_state_label.text() == "状態: 動いています"
    assert screen.freshclam_boot_label.text() == "PC の起動時に自動で起動する: する"
    qtbot.waitUntil(lambda: screen.freshclam_stop_button.isEnabled())
    assert not screen.freshclam_start_button.isEnabled()
    screen.freshclam_stop_button.click()
    qtbot.waitUntil(lambda: screen.freshclam_state_label.text() == "状態: 止まっています")
    fake.set_running.assert_called_once_with(False)
    assert screen.freshclam_summary_label.text() == "ClamQtPie が自動更新します"
    assert screen.freshclam_start_button.isEnabled() and not screen.freshclam_stop_button.isEnabled()


def test_update_screen_shows_a_failure(qtbot, monkeypatch):
    shown = []
    monkeypatch.setattr("gui.screens.update.show_error_dialog", lambda parent, error: shown.append(error))
    fake = MagicMock()
    fake.status.return_value = {"available": True, "active": True, "enabled": True, "summary": ""}
    fake.set_running.side_effect = DefinitionUpdateError("停止できませんでした")
    screen = _screen(qtbot, fake)
    screen.apply_freshclam_state(fake.status.return_value)
    screen.freshclam_stop_button.click()
    qtbot.waitUntil(lambda: bool(shown))
    assert shown[0].code == "E8004"
