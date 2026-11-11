"""WebUI・ほかのトレイGUI で変えた一覧を、開いている画面に出す

- サービスは、Web API で一覧などを変えたら data.changed（変えた対象）を出す
- トレイGUI は data.changed を受けたら、その一覧を読み直す（保存していない変更は残す）
- サービスに無くなった行への保存していない変更は捨てる（保存すると「対象が見つかりません」になるため）
- WebUI も、開いている画面が対象なら読み直す
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.bridge import EventHub
from api.events import DATA_CHANGED_TARGETS, data_changed_publisher

WEB = Path(__file__).resolve().parents[2] / "web"


def test_service_announces_what_was_changed():
    hub = EventHub()
    publish = data_changed_publisher(hub)
    for what in ("targets", "excludes", "config", "scan_started"):
        publish(what)
    # 設定・スキャンの開始は、それぞれ config.changed・scan.started で知らせるため重ねない
    assert [(e.name, e.data) for e in hub.events_since(0)] == [
        ("data.changed", {"what": "targets"}), ("data.changed", {"what": "excludes"})]


def test_every_change_the_api_makes_is_announced_or_has_its_own_event():
    from api import actions

    names = {value for key, value in vars(actions).items() if key.startswith("CHANGED_")}
    own_event = {"config", "scan_started", "update_started", "realtime"}  # config.changed・scan.started・update.started・status
    assert names - own_event == DATA_CHANGED_TARGETS


def test_service_api_wires_the_announcement(qapp, monkeypatch):
    import api.service_api as service_api

    captured = {}

    class _Stop(Exception):
        pass

    def fake_context(**kwargs):
        captured.update(kwargs)
        raise _Stop

    monkeypatch.setattr(service_api, "ApiContext", fake_context)
    api = service_api.ServiceApi(MagicMock())
    with pytest.raises(_Stop):
        api.start()
    captured["notify_changed"]("targets")
    assert [(e.name, e.data) for e in api.events.events_since(0)][-1] == ("data.changed", {"what": "targets"})


class _Window:
    def __init__(self):
        self.screens = {name: MagicMock() for name in
                        ("realtime", "schedule", "quarantine", "home", "log", "update")}

    def get_screen(self, name):
        return self.screens[name]


@pytest.mark.parametrize(("what", "screen", "method"), [
    ("targets", "realtime", "reload_targets"),
    ("excludes", "realtime", "reload_excludes"),
    ("schedules", "schedule", "reload"),
    ("quarantine", "quarantine", "reload"),
    ("logs", "log", "reload"),
    ("clamd", "home", "refresh_status"),
    ("freshclam", "update", "reload_freshclam_service"),
])
def test_tray_reloads_the_changed_list(what, screen, method):
    import tray_main

    window = _Window()
    tray_main.reload_changed_screens(window, what)
    getattr(window.screens[screen], method).assert_called_once()


def test_every_announced_change_is_handled_by_the_tray():
    import tray_main

    for what in DATA_CHANGED_TARGETS:
        window = _Window()
        tray_main.reload_changed_screens(window, what)
        assert any(screen.method_calls for screen in window.screens.values()), what


def _realtime_screen(qtbot, monkeypatch, targets):
    from tests.unit.test_admin_screens import _Batch, _realtime

    screen, _manager, service, _excludes = _realtime(qtbot, _Batch(), monkeypatch)
    service.list_all.return_value = targets
    return screen, service


def test_reload_keeps_unsaved_changes_and_shows_new_folders(qtbot, monkeypatch):
    import gui.screens.realtime as realtime_module

    a = SimpleNamespace(id=1, path="/home/u/a", enabled=True)
    screen, service = _realtime_screen(qtbot, monkeypatch, [a])
    screen.reload_targets()
    monkeypatch.setattr(realtime_module.QFileDialog, "getExistingDirectory", lambda *a, **k: "/home/u/new")
    screen.add_target_button.click()

    # WebUI で /home/u/b を足した
    service.list_all.return_value = [a, SimpleNamespace(id=2, path="/home/u/b", enabled=True)]
    screen.reload_targets()
    paths = [screen.target_table.item(row, 0).text() for row in range(screen.target_table.rowCount())]
    assert paths == ["/home/u/a", "/home/u/b", "/home/u/new"]
    assert "1 件" in screen.save_bar.label.text()


def test_changes_to_rows_removed_elsewhere_are_dropped(qtbot, monkeypatch):
    a = SimpleNamespace(id=1, path="/home/u/a", enabled=True)
    b = SimpleNamespace(id=2, path="/home/u/b", enabled=True)
    screen, service = _realtime_screen(qtbot, monkeypatch, [a, b])
    screen.reload_targets()
    screen.target_table.selectRow(1)
    screen.toggle_target_button.click()
    assert screen.has_unsaved_changes()

    service.list_all.return_value = [a]  # WebUI で /home/u/b を外した
    screen.reload_targets()
    assert not screen.has_unsaved_changes()
    assert screen.pending_operations() == []


def test_pending_edits_prune():
    from gui.pending_edits import PendingEdits

    pending = PendingEdits()
    pending.add({"path": "/new"})
    pending.remove(3)
    pending.toggle(4, saved_enabled=True)
    pending.update(5, {"name": "x"})
    pending.prune({4})
    assert pending.removed == set() and pending.enabled == {4: False} and pending.updated == {}
    assert len(pending.added) == 1  # 保存前の追加は残す


def test_webui_reloads_the_open_screen_on_data_changed():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert '"data.changed"' in js
    for what in DATA_CHANGED_TARGETS:
        assert f"  {what}: [" in js, what
