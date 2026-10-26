"""
gui/screens/realtime.py のサービス層結線に関するテスト
"""
from dataclasses import dataclass
from unittest.mock import MagicMock

from gui.screens.realtime import RealtimeScreen


@dataclass
class _FakeTarget:
    id: int
    path: str
    enabled: bool


@dataclass
class _FakeExclude:
    id: int
    type: str
    value: str
    enabled: bool


def _make_screen(qtbot):
    screen = RealtimeScreen()
    qtbot.addWidget(screen)
    return screen


def test_bind_services_loads_targets_and_excludes(qtbot):
    screen = _make_screen(qtbot)
    manager = MagicMock()
    manager.config.default_action = "quarantine"
    watch_target_service = MagicMock()
    watch_target_service.list_all.return_value = [_FakeTarget(1, "/tmp/watched", True)]
    exclude_service = MagicMock()
    exclude_service.list_all.return_value = [_FakeExclude(1, "extension", ".log", True)]

    screen.bind_services(manager, watch_target_service, exclude_service)

    assert screen.target_table.rowCount() == 1
    assert screen.target_table.item(0, 0).text() == "/tmp/watched"
    assert screen._exclude_tables["extension"].rowCount() == 1
    assert screen.action_quarantine_radio.isChecked() is True


def test_toggle_start_failure_shows_warning_and_unchecks(qtbot, monkeypatch):
    screen = _make_screen(qtbot)
    manager = MagicMock()
    manager.config.default_action = "quarantine"
    manager.start.return_value = False  # 監視対象0件を想定
    watch_target_service = MagicMock()
    watch_target_service.list_all.return_value = []
    watch_target_service.list_enabled_paths.return_value = []
    exclude_service = MagicMock()
    exclude_service.list_all.return_value = []
    screen.bind_services(manager, watch_target_service, exclude_service)

    warned = []
    import gui.screens.realtime as realtime_module

    monkeypatch.setattr(realtime_module, "_show_no_target_warning", lambda *a: warned.append(True))

    screen.toggle_button.setChecked(True)
    screen._on_toggle_clicked(True)

    manager.start.assert_called_once()
    assert warned == [True]
    assert screen.toggle_button.isChecked() is False


def test_toggle_start_failure_with_targets_does_not_show_no_target_warning(qtbot, monkeypatch):
    # clamonacc方式の起動失敗（認証キャンセル等）は監視対象があっても起こる。
    # その場合のエラーはscan_errorで表示されるため「監視対象なし」警告は出さない
    screen = _make_screen(qtbot)
    manager = MagicMock()
    manager.config.default_action = "quarantine"
    manager.start.return_value = False
    watch_target_service = MagicMock()
    watch_target_service.list_all.return_value = []
    watch_target_service.list_enabled_paths.return_value = ["/home/user"]
    exclude_service = MagicMock()
    exclude_service.list_all.return_value = []
    screen.bind_services(manager, watch_target_service, exclude_service)

    warned = []
    import gui.screens.realtime as realtime_module

    monkeypatch.setattr(realtime_module, "_show_no_target_warning", lambda *a: warned.append(True))

    screen.toggle_button.setChecked(True)
    screen._on_toggle_clicked(True)

    assert warned == []
    assert screen.toggle_button.isChecked() is False


def test_mode_label_shows_manager_mode_name(qtbot):
    screen = _make_screen(qtbot)
    manager = MagicMock()
    manager.config.default_action = "quarantine"
    manager.mode_name = "clamonacc"
    watch_target_service = MagicMock()
    watch_target_service.list_all.return_value = []
    exclude_service = MagicMock()
    exclude_service.list_all.return_value = []

    screen.bind_services(manager, watch_target_service, exclude_service)

    assert screen.mode_label.text() == "方式: clamonacc"


def test_toggle_stop_requires_confirmation(qtbot, monkeypatch):
    screen = _make_screen(qtbot)
    manager = MagicMock()
    manager.config.default_action = "quarantine"
    watch_target_service = MagicMock()
    watch_target_service.list_all.return_value = []
    exclude_service = MagicMock()
    exclude_service.list_all.return_value = []
    screen.bind_services(manager, watch_target_service, exclude_service)

    import gui.screens.realtime as realtime_module

    monkeypatch.setattr(realtime_module, "show_confirm_dialog", lambda *a, **k: False)

    screen.toggle_button.setChecked(True)  # 監視中の状態を模擬
    screen._on_toggle_clicked(False)

    manager.stop.assert_not_called()
    assert screen.toggle_button.isChecked() is True  # キャンセルなので元に戻る


def test_action_radio_updates_manager_config(qtbot):
    screen = _make_screen(qtbot)
    manager = MagicMock()
    manager.config.default_action = "quarantine"
    watch_target_service = MagicMock()
    watch_target_service.list_all.return_value = []
    exclude_service = MagicMock()
    exclude_service.list_all.return_value = []
    screen.bind_services(manager, watch_target_service, exclude_service)

    screen.action_notify_only_radio.setChecked(True)

    assert manager.config.default_action == "notify_only"


# -- 除外設定の追加／削除／有効-無効（画面設計書3章） ------------------------------------
import pytest  # noqa: E402

import gui.screens.realtime as realtime_module  # noqa: E402


@pytest.fixture
def exclude_env(qtbot, tmp_path):
    from core.exclude_service import ExcludeService
    from db.database import DatabaseService

    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()
    exclude_service = ExcludeService(db_service)
    screen = _make_screen(qtbot)
    manager = MagicMock()
    manager.config.default_action = "quarantine"
    manager.is_running = True
    watch_target_service = MagicMock()
    watch_target_service.list_all.return_value = []
    screen.bind_services(manager, watch_target_service, exclude_service)
    yield screen, exclude_service, manager
    db_service.close()


def _select_tab(screen, type_key):
    keys = [k for _, k in realtime_module._EXCLUDE_TYPES]
    screen.exclude_tabs.setCurrentIndex(keys.index(type_key))


def test_add_folder_exclude_via_dialog_and_notify_manager(exclude_env, monkeypatch):
    screen, exclude_service, manager = exclude_env
    monkeypatch.setattr(realtime_module.QFileDialog, "getExistingDirectory", lambda *a, **k: "/data/cache")
    _select_tab(screen, "folder")

    screen.add_exclude_button.click()

    assert [(r.type, r.value, bool(r.enabled)) for r in exclude_service.list_all()] == [
        ("folder", "/data/cache", True)
    ]
    assert screen._exclude_tables["folder"].rowCount() == 1
    manager.on_excludes_changed.assert_called_once_with("folder")


def test_add_extension_normalizes_and_skips_duplicates(exclude_env, monkeypatch):
    screen, exclude_service, _ = exclude_env
    monkeypatch.setattr(realtime_module.QInputDialog, "getText", lambda *a, **k: ("LOG", True))
    _select_tab(screen, "extension")

    screen.add_exclude_button.click()
    screen.add_exclude_button.click()  # 同じ値は重複登録しない

    assert [r.value for r in exclude_service.list_all()] == [".log"]


def test_add_cancelled_does_nothing(exclude_env, monkeypatch):
    screen, exclude_service, manager = exclude_env
    monkeypatch.setattr(realtime_module.QInputDialog, "getText", lambda *a, **k: ("", False))
    _select_tab(screen, "process")

    screen.add_exclude_button.click()

    assert exclude_service.list_all() == []
    manager.on_excludes_changed.assert_not_called()


def test_toggle_and_remove_exclude(exclude_env, monkeypatch):
    screen, exclude_service, _ = exclude_env
    exclude_service.add("file", "/home/user/a.txt")
    screen.reload_excludes()
    _select_tab(screen, "file")
    table = screen._exclude_tables["file"]
    table.selectRow(0)

    screen.toggle_exclude_button.click()
    assert bool(exclude_service.list_all()[0].enabled) is False
    assert table.item(0, 1).text() == "無効"

    monkeypatch.setattr(realtime_module, "show_confirm_dialog", lambda *a, **k: True)
    table.selectRow(0)
    screen.remove_exclude_button.click()
    assert exclude_service.list_all() == []


# -- Phase 11: 監視対象の有効/無効・0件時の無効化・初回案内・E2001通知・状態同期 -----------------
@pytest.fixture
def target_env(qtbot, tmp_path):
    from core.watch_target_service import WatchTargetService
    from db.database import DatabaseService

    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()
    watch_target_service = WatchTargetService(db_service)
    screen = _make_screen(qtbot)
    manager = MagicMock()
    manager.config.default_action = "quarantine"
    manager.is_running = False
    exclude_service = MagicMock()
    exclude_service.list_all.return_value = []
    screen.bind_services(manager, watch_target_service, exclude_service)
    yield screen, watch_target_service, manager
    db_service.close()


def test_toggle_button_disabled_without_enabled_targets(target_env):
    screen, watch_target_service, _ = target_env
    assert screen.toggle_button.isEnabled() is False
    assert "追加" in screen.toggle_button.toolTip()

    watch_target_service.add("/data")
    screen.reload_targets()
    assert screen.toggle_button.isEnabled() is True


def test_toggle_target_applies_diff_while_running(target_env):
    screen, watch_target_service, manager = target_env
    watch_target_service.add("/data")
    manager.is_running = True
    screen.reload_targets()
    screen.target_table.selectRow(0)

    screen.toggle_target_button.click()
    assert watch_target_service.list_enabled_paths() == []
    manager.remove_target.assert_called_once_with("/data")

    screen.target_table.selectRow(0)
    screen.toggle_target_button.click()
    manager.add_target.assert_called_once_with("/data")


def test_watch_target_lost_shows_e2001(target_env, monkeypatch):
    screen, _, _ = target_env
    shown = []
    monkeypatch.setattr(realtime_module, "show_error_dialog", lambda parent, err, *a, **k: shown.append(err))

    screen._on_watch_target_lost("/gone")

    assert shown[0].code == "E2001"
    assert shown[0].detail == "/gone"


def test_first_show_guides_only_once_when_no_targets(target_env, monkeypatch):
    screen, _, _ = target_env
    asked = []
    monkeypatch.setattr(realtime_module, "_ask_add_target_now", lambda parent: asked.append(True) or False)

    screen.show()
    screen.hide()
    screen.show()

    assert asked == [True]


def test_sync_toggle_state_follows_other_screens(target_env):
    screen, watch_target_service, _ = target_env
    watch_target_service.add("/data")

    screen.sync_toggle_state(True)
    assert screen.toggle_button.isChecked() is True
    assert screen.toggle_button.text() == "監視を停止する"

    screen.sync_toggle_state(False)
    assert screen.toggle_button.text() == "監視を開始する"
