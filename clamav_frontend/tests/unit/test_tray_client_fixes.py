"""トレイGUI（段階 12-4b-1）の Linux 実機の確認で見つかった点の修正のテスト"""
from __future__ import annotations

import logging
from datetime import datetime
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
    connected = _Signal()


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
    # 定義は今日の日付（古い定義の表示にならないように。clamd の書式は英語の曜日・月）
    now = datetime.now()
    day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[now.weekday()]
    month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[now.month - 1]
    clamav.get_version.return_value = ("ClamAV 1.5.4", "28172", f"{day} {month} {now.day} {now:%H:%M:%S %Y}")
    screen.bind_services(clamav, SimpleNamespace(is_running=False), MagicMock(), MagicMock(), MagicMock())
    clamav.ping.reset_mock()

    screen.set_service_down("E8105")
    screen.refresh_status()
    clamav.ping.assert_not_called()  # clamd が止まったようには表示しない
    assert screen.hero_title_label.text() == "ClamQtPie サービスにつながりません"
    assert screen.clamd_status_card.findChild(QLabel, "value_label").text() == "不明"
    # 最後に読めた定義DB・リアルタイム監視の値も残さない
    assert screen.db_version_card.findChild(QLabel, "value_label").text() == "-"
    assert screen.realtime_status_card.findChild(QLabel, "value_label").text() == "-"

    screen.set_service_down(None)
    screen.refresh_status()
    assert screen.hero_title_label.text() == "スキャンの準備ができています"
    assert screen.db_version_card.findChild(QLabel, "value_label").text().startswith("28172 / ")
    assert screen.realtime_status_card.findChild(QLabel, "value_label").text() == "OFF"


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


def test_tray_tells_main_window_when_service_is_unreachable(qtbot, tmp_path):
    # つながらない・つながったことを、ホーム画面だけでなくメイン画面（右上・下の帯）にも伝える（#80・README_DEV §104）
    from unittest.mock import MagicMock

    import tray_main
    from PySide6.QtWidgets import QApplication
    from core.exceptions import ServiceUnavailableError

    tray = tray_main.TrayApp(QApplication.instance(), SimpleNamespace(api_dir=tmp_path))
    tray.window = MagicMock()
    tray._show_unreachable(ServiceUnavailableError(detail="止まっています"))
    tray.window.set_service_down.assert_called_with("E8105")
    tray._on_connected()
    tray.window.set_service_down.assert_called_with(None)
    tray.window = None
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


# --- サービスにつながり直したときのスキャンの状態（README_DEV §98） -----------------------------

class _JobApi:
    """GET /scans/current の答えと、中止（POST /scans/current/cancel）の答えを決められる"""

    def __init__(self, job=None, cancel_error=None):
        self.job = job
        self.cancel_error = cancel_error
        self.calls = []

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path))
        if path == "/scans/current":
            return {"job": self.job}
        if path == "/scans/current/cancel" and self.cancel_error is not None:
            raise self.cancel_error
        return {}


def _proxy_with(api):
    proxy = ScanDispatcherProxy(api, _Events())
    finished, started, errors = [], [], []
    proxy.finished.connect(finished.append)
    proxy.started.connect(started.append)
    proxy.error.connect(errors.append)
    return proxy, finished, started, errors


def test_resync_ends_a_scan_the_restarted_service_no_longer_runs(qapp):
    # 起動し直したサービスには実行中のスキャンが無い。画面を「スキャン中」のままにしない
    proxy, finished, _started, _errors = _proxy_with(_JobApi(job=None))
    proxy._running = True
    proxy.resync()
    assert proxy.is_running is False
    assert finished == [{"scanned": 0, "detected": 0, "errors": 0, "cancelled": True,
                         "history_id": None, "state": "cancelled"}]


def test_resync_uses_the_result_of_a_scan_that_ended_while_disconnected(qapp):
    job = {"state": "finished", "label": "フルスキャン", "history_id": 7,
           "progress": {"scanned": 5, "total": 5, "detected": 1, "errors": 0}}
    proxy, finished, _started, _errors = _proxy_with(_JobApi(job=job))
    proxy._running = True
    proxy.resync()
    assert finished[0]["cancelled"] is False and finished[0]["scanned"] == 5 and finished[0]["history_id"] == 7


def test_resync_picks_up_a_scan_started_while_disconnected(qapp):
    job = {"state": "running", "label": "スケジュールスキャン", "progress": {}}
    proxy, finished, started, _errors = _proxy_with(_JobApi(job=job))
    proxy.resync()
    assert proxy.is_running is True and started == ["スケジュールスキャン"] and finished == []
    proxy.resync()  # 状態が合っていれば何もしない
    assert started == ["スケジュールスキャン"]


def test_stop_without_a_running_scan_returns_the_screen_to_idle(qapp):
    # ［停止］で「実行中のスキャンはありません」（E8104）が返ったら、エラーを出さずに状態を合わせる
    from core.exceptions import ApiConflictError

    api = _JobApi(job=None, cancel_error=ApiConflictError("実行中のスキャンはありません"))
    proxy, finished, _started, errors = _proxy_with(api)
    proxy._running = True
    proxy.stop()
    assert proxy.is_running is False and len(finished) == 1 and errors == []


def test_stop_error_is_shown_when_the_state_cannot_be_checked(qapp):
    from core.exceptions import ApiConflictError, ServiceUnavailableError

    class _Down(_JobApi):
        def call(self, method, path, body=None, params=None):
            if path == "/scans/current":
                raise ServiceUnavailableError()
            return super().call(method, path, body, params)

    proxy, finished, _started, errors = _proxy_with(_Down(cancel_error=ApiConflictError()))
    proxy._running = True
    proxy.stop()
    assert proxy.is_running is True and finished == [] and [e.code for e in errors] == ["E8104"]


def test_resync_runs_after_the_event_stream_connects(qtbot, monkeypatch):
    # つながった後、切れていた間のイベントの送り直しを受け取ってから取り直す
    from PySide6.QtCore import QObject, Signal

    class _Stream(QObject):
        event = Signal(str, object)
        connected = Signal()

    monkeypatch.setattr(ScanDispatcherProxy, "RESYNC_DELAY_MS", 10)
    stream = _Stream()
    api = _JobApi(job=None)
    proxy = ScanDispatcherProxy(api, stream)
    proxy._running = True
    with qtbot.waitSignal(proxy.finished, timeout=2000):
        stream.connected.emit()
    assert ("GET", "/scans/current") in api.calls
