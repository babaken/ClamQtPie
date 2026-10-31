"""
定義データベースの古さの判定（core/definitions.py）と、ホーム画面・トレイ・通知での警告のテスト
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from PySide6.QtWidgets import QLabel

from core.config_model import ConfigModel, validate_and_fix
from core.definitions import definitions_age_days, parse_clamd_db_date
from gui.screens.home import HomeScreen


def _asctime(moment: datetime) -> str:
    """clamdのVERSION応答と同じ形（Cのasctime。日が1桁なら空白2つ）"""
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    weekdays = "Mon Tue Wed Thu Fri Sat Sun".split()
    return f"{weekdays[moment.weekday()]} {months[moment.month - 1]} {moment.day:2d} {moment:%H:%M:%S} {moment.year}"


def test_parse_clamd_db_date():
    assert parse_clamd_db_date("Mon Jan  6 08:00:00 2026") == datetime(2026, 1, 6, 8, 0, 0)
    assert parse_clamd_db_date("Tue Oct 27 23:59:59 2026") == datetime(2026, 10, 27, 23, 59, 59)
    assert parse_clamd_db_date("") is None
    assert parse_clamd_db_date("Mon Jan 6 2026") is None  # 時刻が無い
    assert parse_clamd_db_date("Mon Foo  6 08:00:00 2026") is None
    # 範囲外の値は、例外にせず読めない日付として扱う
    assert parse_clamd_db_date("Mon Jan  6 08:00:00 99999999999999999999") is None
    assert parse_clamd_db_date("Mon Jan 99999999999999999999 08:00:00 2026") is None
    assert parse_clamd_db_date("Mon Jan  6 99999999999999999999:00:00 2026") is None


def test_definitions_age_days():
    now = datetime(2026, 10, 27, 12, 0, 0)
    assert definitions_age_days("Mon Oct 26 12:00:00 2026", now) == 1
    assert definitions_age_days("Sat Oct 17 09:00:00 2026", now) == 10
    assert definitions_age_days("Wed Oct 28 00:00:00 2026", now) == 0  # 時計のずれで未来でも負にしない
    assert definitions_age_days("unknown", now) is None


def test_stale_days_is_validated():
    model = ConfigModel()
    model.update.stale_days = 0
    assert validate_and_fix(model, warn=lambda msg: None).update.stale_days == 3
    model.update.stale_days = 7
    assert validate_and_fix(model, warn=lambda msg: None).update.stale_days == 7


def _home_with_db_date(qtbot, db_date: str, stale_days: int = 3) -> HomeScreen:
    screen = HomeScreen()
    qtbot.addWidget(screen)
    clamav_service = MagicMock()
    clamav_service.ping.return_value = True
    clamav_service.get_version.return_value = ("ClamAV 1.5.4", "27801", db_date)
    realtime_manager = MagicMock()
    realtime_manager.is_running = True
    screen.bind_services(
        clamav_service, realtime_manager, MagicMock(), MagicMock(), MagicMock(), stale_days=lambda: stale_days
    )
    return screen


def test_home_warns_when_definitions_are_older_than_setting(qtbot):
    screen = _home_with_db_date(qtbot, _asctime(datetime.now() - timedelta(days=5)))

    assert screen.status.definitions_stale is True
    assert screen.status.definitions_age_days == 5
    assert screen.hero_title_label.text() == "ウイルス定義が古くなっています"
    assert "5 日更新されていません" in screen.hero_detail_label.text()
    assert "（5日前）" in screen.db_version_card.findChild(QLabel, "value_label").text()


def test_home_does_not_warn_for_recent_definitions(qtbot):
    screen = _home_with_db_date(qtbot, _asctime(datetime.now() - timedelta(days=1)))

    assert screen.status.definitions_stale is False
    assert screen.hero_title_label.text() == "保護されています"


def test_home_keeps_updating_status_when_db_date_is_unreadable(qtbot):
    # clamdの応答の日付が読めない値でも、状態の表示（リアルタイム監視の状態・状態の通知）を更新し続ける
    screen = _home_with_db_date(qtbot, "Mon Jan  6 08:00:00 99999999999999999999")
    with qtbot.waitSignal(screen.status_refreshed, timeout=1000):
        screen.refresh_status()

    assert screen.status.clamd_running is True
    assert screen.status.definitions_age_days is None
    assert screen.status.definitions_stale is False
    assert screen.status.realtime_running is True


def test_home_uses_configured_stale_days(qtbot):
    screen = _home_with_db_date(qtbot, _asctime(datetime.now() - timedelta(days=5)), stale_days=7)
    assert screen.status.definitions_stale is False


def test_tray_warning_tooltip(qtbot, tmp_path):
    from core.version import APP_NAME
    from gui.tray import TrayIcon

    tray = TrayIcon(tmp_path)
    tray.set_warning("ウイルス定義が 5 日更新されていません")
    assert tray.toolTip() == f"{APP_NAME}\nウイルス定義が 5 日更新されていません"
    tray.set_warning(None)
    assert tray.toolTip() == APP_NAME


def test_stale_definitions_notification_follows_update_failure_setting():
    from core.notification_service import NotificationService

    config_service = MagicMock()
    config_service.current = ConfigModel()
    service = NotificationService(config_service)
    sent = []
    service._send_desktop_notification = lambda title, body, open_screen=None: sent.append((title, body))

    service.notify_definitions_stale(5)
    config_service.current.notification.on_update_fail = False
    service.notify_definitions_stale(6)

    assert sent == [("ウイルス定義が古くなっています", "定義データベースが 5 日更新されていません。定義更新を実行してください")]
