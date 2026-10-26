"""
ClamAV エンジン サービス画面（gui/screens/engine.py、画面レイアウト刷新で追加）のテスト
"""
from unittest.mock import MagicMock

from gui.screens.engine import EngineScreen
from gui.screens.home import AppStatus


def _bound(qtbot, **kwargs):
    screen = EngineScreen()
    qtbot.addWidget(screen)
    clamav = MagicMock()
    clamav.endpoint = "TCP 127.0.0.1:3310"
    controller = MagicMock()
    controller.service_name = "clamd"
    screen.bind_services(clamav, controller, **kwargs)
    return screen, clamav, controller


def test_buttons_follow_clamd_state(qtbot):
    screen, _, _ = _bound(qtbot, on_start_clamd=MagicMock(return_value=True))

    screen.apply_status(AppStatus(clamd_running=False))
    assert screen.start_button.isEnabled() and not screen.reload_button.isEnabled()
    assert "停止しています" in screen.state_label.text()

    screen.apply_status(
        AppStatus(clamd_running=True, engine_version="ClamAV 1.5.4", db_version="28157", db_date="d")
    )
    assert not screen.start_button.isEnabled()
    assert screen.stop_button.isEnabled() and screen.reload_button.isEnabled()
    assert "ClamAV 1.5.4" in screen.engine_label.text()


def test_start_delegates_and_finish_refreshes(qtbot):
    on_start, on_refresh = MagicMock(return_value=True), MagicMock()
    screen, _, _ = _bound(qtbot, on_start_clamd=on_start, on_refresh=on_refresh)
    screen.apply_status(AppStatus(clamd_running=False))

    screen.start_button.click()
    on_start.assert_called_once()
    assert not screen.start_button.isEnabled()

    screen.on_start_clamd_finished(True, None)
    on_refresh.assert_called_once()
    assert "起動しました" in screen.output_area.toPlainText()


def test_stop_after_confirm_calls_controller_and_on_stop_requested(qtbot, monkeypatch):
    monkeypatch.setattr("gui.screens.engine.show_confirm_dialog", lambda *a: True)
    on_stop_requested, on_stop_failed = MagicMock(), MagicMock()
    screen, _, controller = _bound(
        qtbot, on_stop_requested=on_stop_requested, on_stop_failed=on_stop_failed
    )
    screen.apply_status(AppStatus(clamd_running=True))
    # レビュー指摘: 停止処理中の定期確認で自動起動が走らないよう、停止を始める前に抑止を依頼する
    controller.stop.side_effect = lambda: on_stop_requested.assert_called_once()

    with qtbot.waitSignal(screen._task.finished, timeout=3000):
        screen.stop_button.click()

    controller.stop.assert_called_once()
    on_stop_requested.assert_called_once()
    on_stop_failed.assert_not_called()
    assert "停止しました" in screen.output_area.toPlainText()


def test_stop_failure_calls_on_stop_failed(qtbot, monkeypatch):
    from core.exceptions import ClamdProcessError

    monkeypatch.setattr("gui.screens.engine.show_confirm_dialog", lambda *a: True)
    shown = []
    monkeypatch.setattr("gui.screens.engine.show_error_dialog", lambda _parent, error: shown.append(error))
    on_stop_requested, on_stop_failed = MagicMock(), MagicMock()
    screen, _, controller = _bound(
        qtbot, on_stop_requested=on_stop_requested, on_stop_failed=on_stop_failed
    )
    screen.apply_status(AppStatus(clamd_running=True))
    controller.stop.side_effect = ClamdProcessError(detail="sc stop failed")

    with qtbot.waitSignal(screen._task.finished, timeout=3000):
        screen.stop_button.click()

    on_stop_requested.assert_called_once()
    on_stop_failed.assert_called_once()
    assert len(shown) == 1
    assert "停止できませんでした" in screen.output_area.toPlainText()


def test_stop_not_started_while_another_task_runs_calls_on_stop_failed(qtbot, monkeypatch):
    monkeypatch.setattr("gui.screens.engine.show_confirm_dialog", lambda *a: True)
    on_stop_requested, on_stop_failed = MagicMock(), MagicMock()
    screen, _, controller = _bound(
        qtbot, on_stop_requested=on_stop_requested, on_stop_failed=on_stop_failed
    )
    monkeypatch.setattr(screen._task, "run", lambda *a: False)

    screen._on_stop_clicked()

    controller.stop.assert_not_called()
    on_stop_requested.assert_called_once()
    on_stop_failed.assert_called_once()


def test_stop_cancelled_does_nothing(qtbot, monkeypatch):
    monkeypatch.setattr("gui.screens.engine.show_confirm_dialog", lambda *a: False)
    on_stop_requested = MagicMock()
    screen, _, controller = _bound(qtbot, on_stop_requested=on_stop_requested)
    screen.apply_status(AppStatus(clamd_running=True))

    screen.stop_button.click()
    controller.stop.assert_not_called()
    on_stop_requested.assert_not_called()


def test_ping_reports_version(qtbot):
    screen, clamav, _ = _bound(qtbot)
    clamav.ping.return_value = True
    clamav.get_version.return_value = ("ClamAV 1.5.4", "28157", "Sun Oct 18 2026")

    with qtbot.waitSignal(screen._task.finished, timeout=3000):
        screen.ping_button.click()
    assert "応答あり（PONG）: ClamAV 1.5.4" in screen.output_area.toPlainText()


def test_start_rejected_while_another_attempt_runs(qtbot):
    # レビュー指摘: 自動起動の試行中に押すと「起動中...」のまま戻らなかった
    screen, _, _ = _bound(qtbot, on_start_clamd=MagicMock(return_value=False))
    screen.apply_status(AppStatus(clamd_running=False))

    screen.start_button.click()

    assert screen.start_button.isEnabled()
    assert screen.start_button.text() == "▶ サービスを開始"
    assert "既に実行中" in screen.output_area.toPlainText()


def test_buttons_reenabled_after_task_finishes(qtbot):
    screen, clamav, _ = _bound(qtbot)
    clamav.ping.return_value = False
    screen.apply_status(AppStatus(clamd_running=True))

    with qtbot.waitSignal(screen._task.finished, timeout=3000):
        screen.ping_button.click()
    assert screen.ping_button.isEnabled()
    assert screen.stop_button.isEnabled()
