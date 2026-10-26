"""
システム診断画面（gui/screens/diagnostics.py、画面レイアウト刷新で追加）のテスト
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication

from core.diagnostics_service import LEVEL_NG, CheckResult, DiagnosticsReport
from gui.screens.diagnostics import DiagnosticsScreen


def test_run_shows_report_and_copies_masked(qtbot, monkeypatch):
    report = DiagnosticsReport(
        created_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        sections=[("アプリ", [("アプリのフォルダ", "/home/someone/ClamQt")])],
        checks=[CheckResult(LEVEL_NG, "clamd に接続できません")],
    )
    service = MagicMock()
    service.run.return_value = report
    screen = DiagnosticsScreen()
    qtbot.addWidget(screen)
    assert not screen.copy_masked_button.isEnabled()
    screen.bind_services(service)

    with qtbot.waitSignal(screen._task.finished, timeout=3000):
        screen.run_button.click()

    assert "ClamQtPie 診断レポート" in screen.report_area.toPlainText()
    assert "問題が 1 件" in screen.summary_label.text()
    assert screen.copy_masked_button.isEnabled()
    assert screen.run_button.isEnabled()  # レビュー指摘: 完了後も無効のまま戻らなかった

    monkeypatch.setattr("gui.screens.diagnostics.mask_report", lambda text, extra=None: "MASKED")
    screen.copy_masked_button.click()
    assert QApplication.clipboard().text() == "MASKED"
    screen.copy_full_button.click()
    assert "/home/someone/ClamQt" in QApplication.clipboard().text()
