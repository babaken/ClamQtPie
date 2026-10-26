"""
gui/screens/home.py のサービス層結線テスト
"""
from unittest.mock import MagicMock

from PySide6.QtWidgets import QLabel

from gui.screens.home import HomeScreen


def test_refresh_status_shows_clamd_running(qtbot):
    screen = HomeScreen()
    qtbot.addWidget(screen)

    clamav_service = MagicMock()
    clamav_service.ping.return_value = True
    clamav_service.get_version.return_value = ("ClamAV 1.3.1", "27200", "Mon Jan 6 2026")
    realtime_manager = MagicMock()
    realtime_manager.is_running = False

    screen.bind_services(clamav_service, realtime_manager, MagicMock(), MagicMock(), MagicMock())

    clamd_label = screen.clamd_status_card.findChild(QLabel, "value_label")
    db_label = screen.db_version_card.findChild(QLabel, "value_label")
    assert clamd_label.text() == "稼働中"
    assert "27200" in db_label.text()


def test_refresh_status_shows_clamd_stopped(qtbot):
    screen = HomeScreen()
    qtbot.addWidget(screen)

    clamav_service = MagicMock()
    clamav_service.ping.return_value = False
    realtime_manager = MagicMock()
    realtime_manager.is_running = False

    screen.bind_services(clamav_service, realtime_manager, MagicMock(), MagicMock(), MagicMock())

    clamd_label = screen.clamd_status_card.findChild(QLabel, "value_label")
    assert clamd_label.text() == "停止"


def test_refresh_status_emits_status_for_status_bar_and_tray(qtbot):
    screen = HomeScreen()
    qtbot.addWidget(screen)
    clamav_service = MagicMock()
    clamav_service.ping.return_value = True
    clamav_service.get_version.return_value = ("ClamAV 1.5.4", "28157", "Sun Oct 18 15:24:14 2026")
    realtime_manager = MagicMock()
    realtime_manager.is_running = True
    screen.bind_services(clamav_service, realtime_manager, MagicMock(), MagicMock(), MagicMock())

    with qtbot.waitSignal(screen.status_refreshed, timeout=1000) as blocker:
        screen.refresh_status()

    status = blocker.args[0]
    assert status.clamd_running is True
    assert status.db_version == "28157"
    assert status.db_date == "Sun Oct 18 15:24:14 2026"
    assert status.realtime_running is True


def test_refresh_status_without_clamd_check_skips_ping_and_keeps_last_state(qtbot):
    screen = HomeScreen()
    qtbot.addWidget(screen)
    clamav_service = MagicMock()
    clamav_service.ping.return_value = True
    clamav_service.get_version.return_value = ("ClamAV 1.5.4", "28157", "Sun Oct 18 2026")
    realtime_manager = MagicMock()
    realtime_manager.is_running = False
    screen.bind_services(clamav_service, realtime_manager, MagicMock(), MagicMock(), MagicMock())
    clamav_service.ping.reset_mock()

    realtime_manager.is_running = True  # 別画面から監視を開始した想定
    screen.refresh_status(check_clamd=False)

    clamav_service.ping.assert_not_called()
    assert screen.status.clamd_running is True  # 前回の確認結果を引き継ぐ
    assert screen.status.realtime_running is True
    realtime_label = screen.realtime_status_card.findChild(QLabel, "value_label")
    assert realtime_label.text() == "ON"


def test_quick_scan_button_calls_callback(qtbot):
    screen = HomeScreen()
    qtbot.addWidget(screen)

    on_quick_scan = MagicMock()
    clamav_service = MagicMock()
    clamav_service.ping.return_value = False
    screen.bind_services(clamav_service, MagicMock(), on_quick_scan, MagicMock(), MagicMock())

    screen.scan_now_button.click()

    on_quick_scan.assert_called_once()


def test_update_now_button_calls_callback(qtbot):
    screen = HomeScreen()
    qtbot.addWidget(screen)

    on_update_now = MagicMock()
    clamav_service = MagicMock()
    clamav_service.ping.return_value = False
    screen.bind_services(clamav_service, MagicMock(), MagicMock(), on_update_now, MagicMock())

    screen.update_now_button.click()

    on_update_now.assert_called_once()


def test_toggle_realtime_button_calls_callback_and_refreshes(qtbot):
    screen = HomeScreen()
    qtbot.addWidget(screen)

    on_toggle = MagicMock()
    clamav_service = MagicMock()
    clamav_service.ping.return_value = False
    realtime_manager = MagicMock()
    realtime_manager.is_running = True
    screen.bind_services(clamav_service, realtime_manager, MagicMock(), MagicMock(), on_toggle)

    screen.toggle_realtime_button.click()

    on_toggle.assert_called_once()
    realtime_label = screen.realtime_status_card.findChild(QLabel, "value_label")
    assert realtime_label.text() == "ON"


# -- Phase 11: 直近アクティビティ・画面遷移・clamd起動ボタン ------------------------------
def _log_reader_with(tmp_path, lines):
    from core.log_reader_service import LogReaderService

    log_path = tmp_path / "app.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return LogReaderService(log_path)


def test_activity_shows_latest_business_logs_and_navigates(qtbot, tmp_path):
    lines = [f"2026-10-19 10:00:{i:02d},000 [INFO] <スキャン> scan {i}" for i in range(12)]
    lines += [
        "2026-10-19 11:00:00,000 [INFO] アプリ起動シーケンス完了",  # カテゴリなし（システム）は表示しない
        "2026-10-19 11:00:01,000 [DEBUG] <スキャン> 詳細ログ",  # DEBUGは表示しない
        "2026-10-19 11:00:02,000 [WARNING] <リアルタイム> ウイルスを検知しました: /a (Eicar)",
    ]
    navigated = []
    screen = HomeScreen()
    qtbot.addWidget(screen)
    clamav_service = MagicMock()
    clamav_service.ping.return_value = False
    screen.bind_services(
        clamav_service, MagicMock(is_running=False), MagicMock(), MagicMock(), MagicMock(),
        log_reader_service=_log_reader_with(tmp_path, lines), on_navigate=navigated.append,
    )

    assert screen.activity_table.rowCount() == 10  # 最大10件
    assert screen.activity_table.item(0, 1).text() == "リアルタイム"  # 新しい順
    assert "ウイルスを検知しました" in screen.activity_table.item(0, 2).text()

    screen._on_activity_clicked(0, 0)
    screen.db_version_card.clicked.emit()
    assert navigated == ["realtime", "update"]


def test_hero_shows_stopped_state_and_engine_card_navigates(qtbot):
    # 画面レイアウト刷新: clamdの起動はエンジン サービス画面へ移した（ホームからは遷移のみ）
    navigated = []
    screen = HomeScreen()
    qtbot.addWidget(screen)
    clamav_service = MagicMock()
    clamav_service.ping.return_value = False
    screen.bind_services(
        clamav_service, MagicMock(is_running=False), MagicMock(), MagicMock(), MagicMock(),
        on_navigate=navigated.append,
    )
    assert "停止" in screen.hero_title_label.text()

    screen.engine_button.click()
    screen.clamd_status_card.clicked.emit()
    screen.custom_scan_button.click()
    assert navigated == ["engine", "engine", "scan"]


def test_hero_and_toggle_label_follow_realtime_state(qtbot):
    screen = HomeScreen()
    qtbot.addWidget(screen)
    clamav_service = MagicMock()
    clamav_service.ping.return_value = True
    clamav_service.get_version.return_value = ("ClamAV 1.5.4", "28157", "Sun Oct 18 2026")
    realtime_manager = MagicMock(is_running=True)
    screen.bind_services(clamav_service, realtime_manager, MagicMock(), MagicMock(), MagicMock())

    assert screen.hero_title_label.text() == "保護されています"
    assert screen.toggle_realtime_button.text() == "監視を停止"
    assert screen.status.engine_version == "ClamAV 1.5.4"
