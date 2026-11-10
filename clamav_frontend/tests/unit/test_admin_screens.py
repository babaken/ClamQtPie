"""トレイGUI の admin の画面（段階 12-4c-2）のテスト

除外・監視対象・スケジュールの変更を画面の中にためておき、［変更を保存］で 1 回の昇格にまとめること、
設定画面の保護された項目・保存の結果、保存していない変更がある画面を離れるときの確認。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api_client.admin_ops import Operation
from core.config_model import ConfigModel
from core.exceptions import ApiNotFoundError, ElevationCancelledError
from core.scheduler_service import ScheduleRecord
from gui.pending_edits import PendingEdits


class _Batch:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, operations):
        self.calls.append([(op.method, op.path, op.body) for op in operations])
        if self.error is not None:
            raise self.error
        return [None] * len(operations)


# -- ためておく変更 ------------------------------------------------------------------------------
def test_pending_edits_can_be_undone():
    pending = PendingEdits()
    pending.add({"path": "/a"})
    pending.toggle(3, saved_enabled=True)
    pending.remove(4)
    assert len(pending) == 3
    assert pending.state_text(-1, True) == "追加（保存前）"
    assert pending.state_text(3, True) == "無効（保存前）" and pending.state_text(4, True) == "削除（保存前）"
    pending.toggle(3, saved_enabled=True)  # 2 回切り替えたら元に戻る
    pending.remove(4)  # 削除の予定の行をもう一度選べば、削除をやめる
    pending.remove(-1)  # 保存していない追加は取り消す
    assert len(pending) == 0 and pending.state_text(3, True) == "有効"


# -- リアルタイム保護の画面 ----------------------------------------------------------------------
def _realtime(qtbot, batch, monkeypatch):
    from gui.screens.realtime import RealtimeScreen

    screen = RealtimeScreen()
    qtbot.addWidget(screen)
    manager = MagicMock()
    manager.config.default_action = "quarantine"
    manager.is_running = False
    targets = MagicMock()
    targets.list_all.return_value = [SimpleNamespace(id=1, path="/home/u/a", enabled=True)]
    excludes = MagicMock()
    excludes.list_all.return_value = [SimpleNamespace(id=7, type="extension", value=".log", enabled=True)]
    screen.bind_services(manager, targets, excludes, batch=batch,
                         config_operation=lambda action: Operation("PUT", "/config", {"realtime": {"default_action": action}}))
    monkeypatch.setattr(screen, "showEvent", lambda event: None)
    return screen, manager, targets, excludes


def test_realtime_changes_are_saved_in_one_elevation(qtbot, monkeypatch):
    import gui.screens.realtime as realtime_module

    batch = _Batch()
    screen, manager, targets, excludes = _realtime(qtbot, batch, monkeypatch)
    assert not screen.save_card.isHidden()
    monkeypatch.setattr(realtime_module.QFileDialog, "getExistingDirectory", lambda *a, **k: "/home/u/b")
    screen.add_target_button.click()
    screen.target_table.selectRow(0)
    screen.toggle_target_button.click()
    screen.exclude_tabs.setCurrentIndex(2)  # 拡張子
    screen._exclude_tables["extension"].selectRow(0)
    screen.remove_exclude_button.click()
    screen.action_notify_only_radio.setChecked(True)

    targets.add.assert_not_called()  # その場では変えない
    assert screen.target_table.item(1, 1).text() == "追加（保存前）"
    assert screen.has_unsaved_changes() and "4 件" in screen.save_bar.label.text()

    assert screen.save_changes() is True
    assert batch.calls == [[
        ("DELETE", "/excludes/7", None),
        ("PATCH", "/realtime/targets/1", {"enabled": False}),
        ("POST", "/realtime/targets", {"path": "/home/u/b"}),
        ("PUT", "/config", {"realtime": {"default_action": "notify_only"}}),
    ]]
    assert not screen.has_unsaved_changes() and "4 件の変更を保存しました" in screen.save_bar.label.text()
    manager.add_target.assert_not_called()  # 監視への反映はサービスが行う


def test_folder_to_be_removed_is_shown_with_the_buttons_wording(qtbot, monkeypatch):
    """外す予定の行は「外す（保存前）」と出し、選んでいる間はボタンが［外すのをやめる］になる"""
    import gui.screens.realtime as realtime_module

    screen, _manager, _targets, _excludes = _realtime(qtbot, _Batch(), monkeypatch)
    assert screen.remove_target_button.text() == "フォルダを外す"
    screen.target_table.selectRow(0)
    screen.remove_target_button.click()
    assert screen.target_table.item(0, 1).text() == "外す（保存前）"
    assert screen.remove_target_button.text() == "フォルダを外す"  # 一覧を作り直すと選択が外れる
    screen.target_table.selectRow(0)
    assert screen.remove_target_button.text() == "外すのをやめる"
    screen.remove_target_button.click()
    assert screen.target_table.item(0, 1).text() == "有効" and not screen.has_unsaved_changes()

    monkeypatch.setattr(realtime_module.QFileDialog, "getExistingDirectory", lambda *a, **k: "/home/u/b")
    screen.add_target_button.click()
    screen.target_table.selectRow(1)
    assert screen.remove_target_button.text() == "追加をやめる"
    # 除外の一覧は今までどおり「削除」
    screen.exclude_tabs.setCurrentIndex(2)
    screen._exclude_tables["extension"].selectRow(0)
    screen.remove_exclude_button.click()
    assert screen._exclude_tables["extension"].item(0, 1).text() == "削除（保存前）"


def test_cancelled_save_keeps_the_changes(qtbot, monkeypatch):
    import gui.screens.realtime as realtime_module

    screen, *_ = _realtime(qtbot, _Batch(ElevationCancelledError()), monkeypatch)
    monkeypatch.setattr(realtime_module.QFileDialog, "getExistingDirectory", lambda *a, **k: "/home/u/b")
    screen.add_target_button.click()
    assert screen.save_changes() is False and screen.has_unsaved_changes()


def test_failed_save_shows_what_was_saved(qtbot, monkeypatch):
    import gui.screens.realtime as realtime_module

    error = ApiNotFoundError()
    error.completed = 1
    screen, *_ = _realtime(qtbot, _Batch(error), monkeypatch)
    monkeypatch.setattr(realtime_module, "show_error_dialog", lambda *a: None)
    monkeypatch.setattr(realtime_module.QFileDialog, "getExistingDirectory", lambda *a, **k: "/home/u/b")
    screen.add_target_button.click()
    screen.target_table.selectRow(0)
    screen.remove_target_button.click()
    assert screen.save_changes() is False
    assert "2 件のうち 1 件を保存したところで失敗しました" in screen.save_bar.label.text()
    assert not screen.has_unsaved_changes()  # 一覧はサービスの今の内容に戻す


# -- スケジュールの画面 --------------------------------------------------------------------------
def _job(id_=1, enabled=True, wake=False):
    return ScheduleRecord(id_, "毎晩", enabled, "0 3 * * *", "", "update", "", "2026-11-04T03:00:00+09:00", wake)


def _schedule(qtbot, batch):
    from gui.screens.schedule import ScheduleScreen

    screen = ScheduleScreen()
    qtbot.addWidget(screen)
    service = MagicMock()
    service.list_all.return_value = [_job()]
    screen.bind_services(service, batch=batch)
    return screen, service


def test_schedule_changes_are_saved_in_one_elevation(qtbot, monkeypatch):
    import gui.screens.schedule as schedule_module

    batch = _Batch()
    screen, service = _schedule(qtbot, batch)
    values = iter([
        {"name": "週末", "scan_type": "scan", "target": "/home/u", "cron": "0 4 * * 6", "wake": True},
        {"name": "毎晩", "scan_type": "update", "target": "", "cron": "0 5 * * *", "wake": True},
    ])

    class Dialog:
        def __init__(self, parent=None, initial=None, show_wake=False):
            assert show_wake  # トレイGUI では「スリープ中でも起こして実行する」を出す
            self.initial = initial

        def exec(self):
            return schedule_module.QDialog.DialogCode.Accepted

        def result_values(self):
            return next(values)

    monkeypatch.setattr(schedule_module, "JobEditDialog", Dialog)
    screen.add_job_button.click()
    screen.job_table.selectRow(0)
    screen.edit_job_button.click()
    screen.job_table.selectRow(0)  # 一覧を作り直すと選択が外れる
    screen.toggle_job_button.click()
    service.add_job.assert_not_called()
    assert screen.job_table.item(1, 5).text() == "（保存前）"

    assert screen.save_changes() is True
    assert batch.calls == [[
        ("PUT", "/schedules/1", {"name": "毎晩", "scan_type": "update", "cron": "0 5 * * *", "target": "", "wake": True}),
        ("PATCH", "/schedules/1", {"enabled": False}),
        ("POST", "/schedules", {"name": "週末", "scan_type": "scan", "cron": "0 4 * * 6", "target": "/home/u",
                                "wake": True}),
    ]]


def test_run_now_waits_for_unsaved_changes(qtbot, monkeypatch):
    import gui.screens.schedule as schedule_module

    screen, service = _schedule(qtbot, _Batch())
    shown = []
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *a: shown.append(a[2]))
    screen.job_table.selectRow(0)
    screen.delete_job_button.click()
    screen.job_table.selectRow(0)
    screen.run_now_button.click()
    assert shown and "保存していない変更" in shown[0]
    service.run_now.assert_not_called()
    del schedule_module


# -- 設定画面 ------------------------------------------------------------------------------------
class _ConfigProxy:
    def __init__(self, error=None):
        self.current = ConfigModel()
        self.protected = ["clamd.tcp_host", "update.freshclam_path"]
        self.saved, self.error, self.invalidated = [], error, 0

    def save(self, model, smtp_password=None):
        self.saved.append((model, smtp_password))
        if self.error is not None:
            raise self.error
        self.current = model

    def invalidate(self):
        self.invalidated += 1


def _settings(qtbot, proxy):
    from gui.screens.settings import SettingsScreen

    screen = SettingsScreen()
    qtbot.addWidget(screen)
    screen.bind_services(proxy, MagicMock())
    return screen


def test_protected_settings_are_read_only_with_the_command(qtbot):
    screen = _settings(qtbot, _ConfigProxy())
    assert not screen.tcp_host_edit.isEnabled() and not screen.freshclam_browse_button.isEnabled()
    assert "clamqtpie-ctl config set clamd.tcp_host" in screen.tcp_host_edit.toolTip()
    assert screen.service_name_edit.isEnabled()  # 保護されていない項目は変えられる
    assert not screen.protected_note.isHidden()
    assert screen.tabs.tabText(1) == "スキャン"


def test_settings_save_sends_the_password_together_and_shows_the_result(qtbot):
    proxy = _ConfigProxy()
    screen = _settings(qtbot, proxy)
    screen.quick_targets_list.addItem("/home/u/Downloads")
    screen.stale_days_spin.setValue(9)
    screen.smtp_password_edit.setText("secret")
    screen.save_button.click()
    [(model, password)] = proxy.saved
    assert password == "secret" and model.scan.quick_targets == ["/home/u/Downloads"] and model.update.stale_days == 9
    assert screen.save_status_label.text().startswith("保存しました")


def test_failed_settings_save_restores_the_services_values(qtbot, monkeypatch):
    import gui.screens.settings as settings_module

    monkeypatch.setattr(settings_module, "show_error_dialog", lambda *a: None)
    proxy = _ConfigProxy(ApiNotFoundError())
    screen = _settings(qtbot, proxy)
    screen.stale_days_spin.setValue(9)
    screen.save_button.click()
    assert proxy.invalidated == 1 and screen.stale_days_spin.value() == 3  # 保存できなかった値を残さない
    assert proxy.current.update.stale_days == 3  # 写しを変えたので、元の設定は変わっていない
    assert "サービスの今の設定に戻しました" in screen.save_status_label.text()


# -- 画面を離れるときの確認 ----------------------------------------------------------------------
@pytest.mark.parametrize("choice, expected_screen, saved, discarded", [
    ("cancel", "realtime", 0, 0), ("discard", "home", 0, 1), ("save", "home", 1, 0),
])
def test_leaving_a_screen_with_unsaved_changes_asks(qtbot, monkeypatch, choice, expected_screen, saved, discarded):
    import gui.main_window as main_window_module
    from gui.main_window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    window.navigate_to("realtime")
    screen = window.get_screen("realtime")
    calls = {"save": 0, "discard": 0}
    monkeypatch.setattr(screen, "has_unsaved_changes", lambda: True)
    monkeypatch.setattr(screen, "save_changes", lambda: calls.__setitem__("save", calls["save"] + 1) or True)
    monkeypatch.setattr(screen, "discard_changes", lambda: calls.__setitem__("discard", calls["discard"] + 1))

    class Box:
        Icon = main_window_module.QMessageBox.Icon
        ButtonRole = main_window_module.QMessageBox.ButtonRole

        def __init__(self, parent):
            self.buttons = []

        def setIcon(self, icon): pass  # noqa: E704
        def setWindowTitle(self, title): pass  # noqa: E704
        def setText(self, text): pass  # noqa: E704

        def addButton(self, text, role):
            button = SimpleNamespace(text=text)
            self.buttons.append(button)
            return button

        def exec(self):
            pass

        def clickedButton(self):
            return self.buttons[{"save": 0, "discard": 1, "cancel": 2}[choice]]

    monkeypatch.setattr(main_window_module, "QMessageBox", Box)
    window.navigate_to("home")
    assert window.current_screen_key() == expected_screen
    assert window.stacked_widget.currentWidget() is window.get_screen(expected_screen)
    assert (calls["save"], calls["discard"]) == (saved, discarded)


# -- 本物の API に（昇格ヘルパーは同じプロセスで代わりに動かす） -----------------------------------
def test_realtime_screen_saves_through_the_helper(qtbot, service, monkeypatch):  # noqa: F811
    import gui.screens.realtime as realtime_module
    from api_client.proxies import ServiceProxies
    from gui.screens.realtime import RealtimeScreen
    from tests.unit.test_elevation import _caller

    caller = _caller(service)
    p = ServiceProxies(caller, MagicMock(), gui_path=service.tmp / "gui.json")
    screen = RealtimeScreen()
    qtbot.addWidget(screen)
    monkeypatch.setattr(screen, "showEvent", lambda event: None)
    screen.bind_services(p.realtime, p.watch_targets, p.excludes, batch=caller.batch)
    monkeypatch.setattr(realtime_module.QFileDialog, "getExistingDirectory", lambda *a, **k: str(service.tmp))
    monkeypatch.setattr(realtime_module.QInputDialog, "getText", lambda *a, **k: (".bak", True))
    screen.add_target_button.click()
    screen.exclude_tabs.setCurrentIndex(2)
    screen.add_exclude_button.click()
    assert screen.save_changes() is True
    assert [t.path for t in p.watch_targets.list_all()] == [str(service.tmp)]
    assert [(e.type, e.value) for e in p.excludes.list_all()] == [("extension", ".bak")]
    assert screen.target_table.item(0, 1).text() == "有効"


from tests.unit.test_api_proxies import service  # noqa: E402,F401 - 本物の API を待ち受けるフィクスチャ
