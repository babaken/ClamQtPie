"""
タスクトレイ常駐アプリプロセス設計書4.1節のテスト（×ボタンのトレイ格納挙動）
"""
from unittest.mock import MagicMock

from core.config_service import ConfigService
from gui.main_window import MainWindow


def test_close_event_hides_window_instead_of_closing(qtbot, tmp_path):
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    tray_icon = MagicMock()

    window = MainWindow(tray_icon=tray_icon, config_service=config_service)
    qtbot.addWidget(window)
    window.show()

    window.close()

    assert window.isVisible() is False
    # closeEventでevent.ignore()しているため、ウィジェット自体は破棄されていない
    assert window.windowTitle() == "ClamQtPie"


def test_close_event_shows_tray_hint_only_once(qtbot, tmp_path):
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    tray_icon = MagicMock()

    window = MainWindow(tray_icon=tray_icon, config_service=config_service)
    qtbot.addWidget(window)

    window.close()
    assert tray_icon.showMessage.call_count == 1

    window.show()
    window.close()
    assert tray_icon.showMessage.call_count == 1  # 2回目は表示されない

    reloaded = config_service.load()
    assert reloaded.general.show_tray_hint_shown is True


def test_show_from_tray_makes_window_visible(qtbot, tmp_path):
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    window = MainWindow(tray_icon=MagicMock(), config_service=config_service)
    qtbot.addWidget(window)
    window.close()
    assert window.isVisible() is False

    window.show_from_tray()

    assert window.isVisible() is True
