"""
gui/screens/scan.py のサービス層結線に関するテスト
（テスト仕様書6.1節「GUI操作」の一部を自動テスト化）
"""
from unittest.mock import MagicMock

from gui.screens.scan import ScanScreen


def _make_screen(qtbot):
    screen = ScanScreen()
    qtbot.addWidget(screen)
    return screen


def test_start_click_calls_dispatcher_with_resolved_targets(qtbot, tmp_path):
    screen = _make_screen(qtbot)
    dispatcher = MagicMock()
    dispatcher.is_running = False
    quarantine_service = MagicMock()
    screen.bind_services(dispatcher, quarantine_service)

    screen.custom_radio.setChecked(True)
    screen.custom_path_list.addItem(str(tmp_path))

    screen.start_button.click()

    dispatcher.start.assert_called_once()
    args, kwargs = dispatcher.start.call_args
    assert args[0] == [str(tmp_path)]
    assert screen.start_button.isEnabled() is False
    assert screen.stop_button.isEnabled() is True


def test_stop_click_calls_dispatcher_stop(qtbot):
    screen = _make_screen(qtbot)
    dispatcher = MagicMock()
    screen.bind_services(dispatcher, MagicMock())
    screen.stop_button.setEnabled(True)  # 通常はスキャン中のみ活性化されるボタン

    screen.stop_button.click()

    dispatcher.stop.assert_called_once()


def test_file_scanned_signal_updates_counters_and_table(qtbot):
    screen = _make_screen(qtbot)
    screen.bind_services(MagicMock(), MagicMock())

    screen._on_file_scanned("/tmp/a.txt", "OK", "", "2026-07-14T00:00:00")
    screen._on_file_scanned("/tmp/eicar.txt", "FOUND", "Eicar-Test-Signature", "2026-07-14T00:00:01")

    # 非機能要件2.3節: 件数表示は200ms間隔でまとめて更新される（ファイルごとには再描画しない）
    assert screen.counter_label.text() == "検知件数: 0 / 検査できなかったファイル: 0 / スキャン件数: 0"
    qtbot.waitUntil(lambda: screen.counter_label.text() == "検知件数: 1 / 検査できなかったファイル: 0 / スキャン件数: 2", timeout=1000)
    assert screen.result_table.rowCount() == 1  # FOUNDのみ一覧表示
    assert screen.result_table.item(0, 0).text() == "/tmp/eicar.txt"


def test_finished_resets_button_states(qtbot):
    screen = _make_screen(qtbot)
    screen.bind_services(MagicMock(), MagicMock())
    screen.start_button.setEnabled(False)
    screen.stop_button.setEnabled(True)

    screen._on_finished({"scanned": 3, "detected": 0, "errors": 0, "cancelled": False})

    assert screen.start_button.isEnabled() is True
    assert screen.stop_button.isEnabled() is False


def test_quarantine_button_calls_quarantine_service_for_selected_row(qtbot, monkeypatch):
    screen = _make_screen(qtbot)
    quarantine_service = MagicMock()
    screen.bind_services(MagicMock(), quarantine_service)

    screen._on_file_scanned("/tmp/eicar.txt", "FOUND", "Eicar-Test-Signature", "2026-07-14T00:00:00")
    screen.result_table.selectRow(0)

    # 確認ダイアログは常にYesを返すようモック化
    import gui.screens.scan as scan_module

    monkeypatch.setattr(scan_module, "show_confirm_dialog", lambda *a, **k: True)

    screen.quarantine_button.click()

    quarantine_service.quarantine.assert_called_once_with("/tmp/eicar.txt", "Eicar-Test-Signature")
    assert screen.result_table.item(0, 1).text() == "隔離済み"


# -- Phase 11: 対象の追加/削除・存在確認・エラー表示・無視・状態表示 --------------------------
import gui.screens.scan as scan_module  # noqa: E402


def _bound_screen(qtbot):
    screen = _make_screen(qtbot)
    dispatcher = MagicMock()
    dispatcher.is_running = False
    screen.bind_services(dispatcher, MagicMock())
    return screen, dispatcher


def test_add_files_and_remove_custom_path(qtbot, monkeypatch, tmp_path):
    screen, _ = _bound_screen(qtbot)
    a, b = tmp_path / "a.exe", tmp_path / "b.exe"
    monkeypatch.setattr(
        scan_module.QFileDialog, "getOpenFileNames", lambda *a_, **k: ([str(a), str(b), str(a)], "")
    )

    screen.add_file_button.click()
    assert [screen.custom_path_list.item(i).text() for i in range(screen.custom_path_list.count())] == [
        str(a),
        str(b),
    ]
    assert screen.custom_radio.isChecked()

    screen.custom_path_list.item(0).setSelected(True)
    screen.remove_path_button.click()
    assert screen.custom_path_list.count() == 1


def test_missing_custom_path_is_rejected(qtbot, monkeypatch, tmp_path):
    screen, dispatcher = _bound_screen(qtbot)
    warnings = []
    monkeypatch.setattr(scan_module.QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    screen.custom_radio.setChecked(True)
    screen.custom_path_list.addItem(str(tmp_path / "missing"))

    screen.start_button.click()

    dispatcher.start.assert_not_called()
    assert "見つかりません" in warnings[0]


def test_full_scan_uses_system_drive_and_skip_dirs(qtbot, monkeypatch):
    screen, dispatcher = _bound_screen(qtbot)
    monkeypatch.setattr(scan_module.scan_targets, "full_scan_targets", lambda: [str(scan_module.Path.cwd())])
    monkeypatch.setattr(scan_module.scan_targets, "full_scan_skip_dirs", lambda: ("/proc",))
    screen.full_radio.setChecked(True)

    screen.start_button.click()

    args, kwargs = dispatcher.start.call_args
    assert kwargs["skip_dirs"] == ("/proc",)
    assert kwargs["label"] == "フルスキャン"
    assert screen.state_label.text() == "状態: スキャン中"


def test_error_results_are_listed_and_state_shows_cancelled(qtbot):
    screen, _ = _bound_screen(qtbot)

    screen._on_file_scanned("/a", "OK", "", "t")
    screen._on_file_scanned("/b", "ERROR", "Permission denied.", "t")
    screen._on_finished({"scanned": 2, "detected": 0, "errors": 1, "cancelled": True})

    assert screen.result_table.rowCount() == 1
    assert screen.result_table.item(0, 1).text() == "未検査（権限なし）"
    assert screen.state_label.text().startswith("状態: 中断（検査できなかったファイルが 1 件あります")


def test_ignore_removes_only_detection_rows(qtbot):
    screen, _ = _bound_screen(qtbot)
    screen._on_file_scanned("/virus", "FOUND", "Eicar", "t")
    screen._on_file_scanned("/err", "ERROR", "denied", "t")
    screen.result_table.selectAll()

    screen.ignore_button.click()

    assert screen.result_table.rowCount() == 1
    assert screen.result_table.item(0, 0).text() == "/err"


def test_activity_shows_queued_and_stalled_states(qtbot):
    screen, _ = _bound_screen(qtbot)
    screen.stop_button.setEnabled(True)  # スキャン中

    screen._on_activity("queued", "/a", 0)
    assert "待機中" in screen.state_label.text()

    screen._on_activity("stalled", "/data/big.iso", 25)
    assert "応答待ち（25秒）" in screen.state_label.text()
    assert "big.iso" in screen.state_label.text()
    assert screen.state_label.toolTip() == "/data/big.iso"

    screen._on_activity("scanning", "", 0)
    assert screen.state_label.text() == "状態: スキャン中"

    screen.stop_button.setEnabled(False)  # 終了後に遅れて届いた通知は無視する
    screen._on_activity("stalled", "/x", 30)
    assert screen.state_label.text() == "状態: スキャン中"


def test_unscanned_files_are_labeled_by_reason_and_counted(qtbot):
    # 検査できなかったファイルは理由ごとに表示し、サイズの上限で検査していないものを区別する
    from core.clamav_service import FILE_VANISHED_DETAIL, SIZE_LIMIT_DETAIL_PREFIX

    screen, _ = _bound_screen(qtbot)
    screen._on_file_scanned("/big.zip", "ERROR", f"{SIZE_LIMIT_DETAIL_PREFIX}（Heuristics.Limits.Exceeded.MaxFileSize）", "t")
    screen._on_file_scanned("/gone.tmp", "ERROR", FILE_VANISHED_DETAIL, "t")
    screen._on_file_scanned("/odd", "ERROR", "Can't open file or directory", "t")
    screen._on_finished({"scanned": 3, "detected": 0, "errors": 3, "cancelled": False})

    labels = [screen.result_table.item(r, 1).text() for r in range(screen.result_table.rowCount())]
    assert labels == ["未検査（サイズの上限）", "未検査（削除済み）", "エラー"]
    assert screen.counter_label.text() == "検知件数: 0 / 検査できなかったファイル: 3 / スキャン件数: 3"
    assert "検査できなかったファイルが 3 件" in screen.state_label.text()
    screen.result_table.selectAll()  # 検知以外の行は「隔離する」の対象にならない
    assert screen._selected_detection_rows() == []
