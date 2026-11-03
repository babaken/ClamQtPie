"""トレイGUI（段階 12-4b-1）の Linux 実機の確認で見つかった点の修正のテスト"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from PySide6.QtWidgets import QLabel

from api_client.proxies import ScanDispatcherProxy
from gui.time_text import local_time_text


class _Events:
    class _Signal:
        def connect(self, _slot):
            pass

    event = _Signal()


class _Api:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        return {}


def test_tray_quick_scan_stays_inside_the_users_folder(qapp, tmp_path):
    api = _Api()
    proxy = ScanDispatcherProxy(api, _Events())
    home = Path.home()
    proxy.start([str(home), str(tmp_path.anchor)], scan_type="quick")
    body = api.calls[0][2]
    # 自分のフォルダの外（Linux の /tmp など）は、サービスが user のトークンでは断るため送らない
    assert body["targets"] == [str(home)] and body["source"] == "gui"


def test_scan_count_uses_the_services_progress_and_summary(qtbot):
    from gui.screens.scan import ScanScreen

    screen = ScanScreen()
    qtbot.addWidget(screen)
    screen._show_running()
    screen._on_progress(2, 3, "/home/u/b")  # 問題の無いファイルの結果（file_scanned）は届かない
    screen._flush_view()
    assert "スキャン件数: 2" in screen.counter_label.text()
    screen._on_finished({"scanned": 3, "detected": 0, "errors": 0, "cancelled": False})
    assert "スキャン件数: 3" in screen.counter_label.text()


def test_home_shows_that_the_service_is_unreachable(qtbot):
    from gui.screens.home import HomeScreen

    screen = HomeScreen()
    qtbot.addWidget(screen)
    clamav = MagicMock()
    clamav.ping.return_value = True
    clamav.get_version.return_value = ("ClamAV 1.5.4", "28172", "Mon Nov 2 15:24:10 2026")
    screen.bind_services(clamav, SimpleNamespace(is_running=False), MagicMock(), MagicMock(), MagicMock())
    clamav.ping.reset_mock()

    screen.set_service_down("E8105")
    screen.refresh_status()
    clamav.ping.assert_not_called()  # clamd が止まったようには表示しない
    assert screen.hero_title_label.text() == "ClamQtPie サービスにつながりません"
    assert screen.clamd_status_card.findChild(QLabel, "value_label").text() == "不明"

    screen.set_service_down(None)
    screen.refresh_status()
    assert screen.hero_title_label.text() == "スキャンの準備ができています"


def test_tray_logs_unreachable_once_and_keeps_the_tooltip(qtbot, tmp_path, caplog):
    import tray_main
    from PySide6.QtWidgets import QApplication
    from core.exceptions import ServiceUnavailableError

    tray = tray_main.TrayApp(QApplication.instance(), SimpleNamespace(api_dir=tmp_path))
    with caplog.at_level(logging.WARNING, logger="clamav_frontend"):
        for _ in range(3):
            tray._show_unreachable(ServiceUnavailableError(detail="止まっています"))
    assert caplog.text.count("ClamQtPie サービスにつながりません") == 1  # つなぎ直すたびには残さない
    assert "サービスにつながりません（E8105）" in tray.tray_icon.toolTip()
    tray.shutdown()


def test_forbidden_requests_are_logged_by_the_service(qapp, service, caplog):  # noqa: F811
    import pytest

    from core.exceptions import ApiForbiddenError
    from tests.unit.test_api_proxies import _proxies

    p = _proxies(service)
    with caplog.at_level(logging.WARNING, logger="clamav_frontend"), pytest.raises(ApiForbiddenError):
        p.excludes.add("extension", ".tmp")
    # エラーのダイアログの「詳細を見る」（エラーコードでログを探す）で見つかるように
    assert "権限が足りないため断りました: POST /api/v1/excludes" in caplog.text


def test_times_are_shown_in_local_time():
    from datetime import datetime, timezone

    utc = datetime(2026, 11, 2, 23, 32, 31, 106391, tzinfo=timezone.utc)
    assert local_time_text(utc.isoformat()) == utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert local_time_text("不明") == "不明" and local_time_text("") == ""


from tests.unit.test_api_proxies import service  # noqa: E402,F401 - 本物の API を待ち受けるフィクスチャ
