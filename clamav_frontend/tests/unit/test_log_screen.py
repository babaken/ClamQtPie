"""
gui/screens/log.py のサービス層結線テスト
"""
from core.log_reader_service import LogReaderService
from gui.screens.log import LogScreen

_SAMPLE_LOG = """\
2026-07-14 10:00:00,123 [INFO] アプリ起動シーケンス開始
2026-07-14 10:00:05,456 [ERROR] [E1001] ClamAVサービスに接続できません
2026-07-14 10:05:00,789 [WARNING] [E7001] メール通知の送信に失敗しました
"""


def _wide_date_range(screen) -> None:
    from PySide6.QtCore import QDate

    screen.date_from_edit.setDate(QDate(2020, 1, 1))
    screen.date_to_edit.setDate(QDate(2030, 12, 31))


def test_log_screen_loads_entries(qtbot, tmp_path):
    log_path = tmp_path / "app.log"
    log_path.write_text(_SAMPLE_LOG, encoding="utf-8")
    service = LogReaderService(log_path)

    screen = LogScreen()
    qtbot.addWidget(screen)
    _wide_date_range(screen)
    screen.bind_services(service)

    assert screen.log_table.rowCount() == 3


def test_log_screen_level_filter_updates_table(qtbot, tmp_path):
    log_path = tmp_path / "app.log"
    log_path.write_text(_SAMPLE_LOG, encoding="utf-8")
    service = LogReaderService(log_path)

    screen = LogScreen()
    qtbot.addWidget(screen)
    _wide_date_range(screen)
    screen.bind_services(service)

    screen.level_checks["INFO"].setChecked(False)
    screen.level_checks["WARNING"].setChecked(False)

    assert screen.log_table.rowCount() == 1
    assert screen.log_table.item(0, 3).text() == "ClamAVサービスに接続できません"


def test_log_screen_keyword_filter(qtbot, tmp_path):
    log_path = tmp_path / "app.log"
    log_path.write_text(_SAMPLE_LOG, encoding="utf-8")
    service = LogReaderService(log_path)

    screen = LogScreen()
    qtbot.addWidget(screen)
    _wide_date_range(screen)
    screen.bind_services(service)

    screen.search_edit.setText("E7001")

    assert screen.log_table.rowCount() == 1
    assert "メール通知" in screen.log_table.item(0, 3).text()


def test_log_screen_filter_by_error_code_sets_search_field(qtbot, tmp_path):
    screen = LogScreen()
    qtbot.addWidget(screen)

    screen.filter_by_error_code("E1001")

    assert screen.search_edit.text() == "E1001"


def test_log_screen_export_csv_calls_service(qtbot, tmp_path, monkeypatch):
    log_path = tmp_path / "app.log"
    log_path.write_text(_SAMPLE_LOG, encoding="utf-8")
    service = LogReaderService(log_path)

    screen = LogScreen()
    qtbot.addWidget(screen)
    _wide_date_range(screen)
    screen.bind_services(service)

    output_path = tmp_path / "out.csv"
    import gui.screens.log as log_module

    monkeypatch.setattr(
        log_module.QFileDialog, "getSaveFileName", lambda *a, **k: (str(output_path), "")
    )
    monkeypatch.setattr(log_module.QMessageBox, "information", lambda *a, **k: None)

    screen.export_csv_button.click()

    assert output_path.exists()


def test_category_filter(qtbot, tmp_path):
    from core.log_reader_service import LogReaderService

    log_path = tmp_path / "app.log"
    today = __import__("datetime").date.today().strftime("%Y-%m-%d")
    log_path.write_text(
        f"{today} 10:00:00,000 [WARNING] <スキャン> ウイルスを検知しました: /a (Eicar)\n"
        f"{today} 10:00:01,000 [INFO] <隔離> ファイルを隔離しました: /a\n"
        f"{today} 10:00:02,000 [INFO] アプリ起動シーケンス完了\n",
        encoding="utf-8",
    )
    screen = LogScreen()
    qtbot.addWidget(screen)
    screen.bind_services(LogReaderService(log_path))
    assert screen.log_table.rowCount() == 3

    screen.category_checks["システム"].setChecked(False)
    screen.category_checks["隔離"].setChecked(False)

    assert screen.log_table.rowCount() == 1
    assert screen.log_table.item(0, 2).text() == "スキャン"


def _bind_real_log(qtbot, tmp_path):
    """実際のログ出力先（setup_logger）と同じファイルを読むログ画面"""
    from core.logger import setup_logger

    log_dir = tmp_path / "logs"
    logger = setup_logger(log_dir, level="DEBUG", retention_days=30)
    logger.info("クリア前の行1")
    logger.warning("クリア前の行2")
    (log_dir / "app.log.2026-10-19").write_text("古い行\n", encoding="utf-8")
    for h in logger.handlers:
        h.flush()
    screen = LogScreen()
    qtbot.addWidget(screen)
    _wide_date_range(screen)
    screen.bind_services(LogReaderService(log_dir / "app.log"))
    return screen, log_dir, logger


def test_clear_button_clears_logs_after_confirmation(qtbot, tmp_path, monkeypatch):
    screen, log_dir, logger = _bind_real_log(qtbot, tmp_path)
    assert screen.log_table.rowCount() == 2
    asked = []
    monkeypatch.setattr("gui.screens.log.show_confirm_dialog", lambda *a: asked.append(a[2]) or True)

    screen.clear_button.click()
    for h in logger.handlers:
        h.flush()
    screen.reload()

    assert "元に戻せません" in asked[0]
    assert screen.log_table.rowCount() == 1  # 「ログをクリアしました」だけ
    assert "ログをクリアしました" in screen.log_table.item(0, 3).text()
    assert not (log_dir / "app.log.2026-10-19").exists()


def test_clear_button_does_nothing_when_cancelled(qtbot, tmp_path, monkeypatch):
    screen, log_dir, _ = _bind_real_log(qtbot, tmp_path)
    monkeypatch.setattr("gui.screens.log.show_confirm_dialog", lambda *a: False)

    screen.clear_button.click()

    assert screen.log_table.rowCount() == 2
    assert (log_dir / "app.log.2026-10-19").exists()

def test_on_shown_reloads_and_moves_period_to_today(qtbot, tmp_path):
    # レビュー指摘: 常駐していると、ログ画面が起動時のログのまま・期間の終了日も起動した日のままだった
    from PySide6.QtCore import QDate

    log_path = tmp_path / "app.log"
    log_path.write_text("", encoding="utf-8")
    screen = LogScreen()
    qtbot.addWidget(screen)
    screen.bind_services(LogReaderService(log_path))
    assert screen.log_table.rowCount() == 0

    # 起動したのが2日前で、期間を変えていない状態にする
    today = QDate.currentDate()
    started = today.addDays(-2)
    screen._period_base_date = started
    screen.date_to_edit.setDate(started)
    screen.date_from_edit.setDate(started.addDays(-7))
    now = today.toString("yyyy-MM-dd")
    log_path.write_text(f"{now} 09:00:00,000 [INFO] <スキャン> 今日のログ\n", encoding="utf-8")

    screen.on_shown()

    assert screen.date_to_edit.date() == today
    assert screen.date_from_edit.date() == today.addDays(-7)
    assert screen.log_table.rowCount() == 1


def test_on_shown_keeps_period_changed_by_user(qtbot, tmp_path):
    from PySide6.QtCore import QDate

    log_path = tmp_path / "app.log"
    log_path.write_text("", encoding="utf-8")
    screen = LogScreen()
    qtbot.addWidget(screen)
    screen.bind_services(LogReaderService(log_path))
    screen._period_base_date = QDate.currentDate().addDays(-2)
    screen.date_to_edit.setDate(QDate(2026, 1, 31))  # 利用者が終了日を変えた

    screen.on_shown()
    assert screen.date_to_edit.date() == QDate(2026, 1, 31)
