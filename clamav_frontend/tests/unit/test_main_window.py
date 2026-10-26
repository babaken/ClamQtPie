"""
テスト仕様書6.1節「GUI操作」の自動化可能な範囲をカバー:
- 10画面すべての表示・遷移（画面レイアウト刷新でエンジン サービス・システム診断を追加）
- エラーダイアログの共通フォーマット
"""
from PySide6.QtWidgets import QMessageBox

from core.exceptions import ClamdConnectionError
from gui.error_dialog import show_error_dialog
from gui.main_window import NAV_ITEMS, MainWindow


def test_status_bar_reflects_status(qtbot):
    from gui.screens.home import AppStatus

    window = MainWindow()
    qtbot.addWidget(window)
    assert window.clamd_status_label.text() == "clamd: 確認中..."

    window.update_status_bar(
        AppStatus(clamd_running=True, db_version="28157", db_date="Sun Oct 18 2026", realtime_running=True)
    )
    assert window.clamd_status_label.text() == "clamd: 稼働中"
    assert window.db_update_label.text() == "定義DB更新: Sun Oct 18 2026"
    assert window.realtime_status_label.text() == "リアルタイム監視: ON"

    window.update_status_bar(AppStatus(clamd_running=False, db_date="Sun Oct 18 2026"))
    assert window.clamd_status_label.text() == "clamd: 停止"
    assert window.db_update_label.text() == "定義DB更新: -"
    assert window.realtime_status_label.text() == "リアルタイム監視: OFF"


def test_all_10_screens_are_registered(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)

    assert window.nav_list.count() == 10
    assert window.stacked_widget.count() == 10
    assert [key for key, _, _ in NAV_ITEMS] == [
        "home",
        "scan",
        "update",
        "log",
        "quarantine",
        "engine",
        "realtime",
        "schedule",
        "diagnostics",
        "settings",
    ]
    assert dict((key, label) for key, label, _ in NAV_ITEMS)["engine"] == "ClamAV エンジン サービス"
    assert dict((key, label) for key, label, _ in NAV_ITEMS)["diagnostics"] == "システム診断"


def test_navigate_to_switches_stacked_widget(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)

    window.navigate_to("settings")

    assert window.stacked_widget.currentWidget().screen_title == "設定"


def test_navigate_to_each_screen_shows_correct_title(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)

    for key, _label, _ in NAV_ITEMS:
        window.navigate_to(key)
        assert window.current_screen_key() == key
        assert window.stacked_widget.currentWidget() is window.get_screen(key)
        assert window.stacked_widget.currentWidget().screen_title  # 各画面にタイトルがある


def test_status_bar_has_three_permanent_labels(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)

    assert "clamd" in window.clamd_status_label.text()
    assert "定義DB" in window.db_update_label.text()
    assert "リアルタイム監視" in window.realtime_status_label.text()


def test_error_dialog_shows_code_and_message(qtbot, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)

    captured = {}

    def fake_exec(self):
        captured["text"] = self.text()
        captured["informative"] = self.informativeText()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)

    err = ClamdConnectionError(detail="Connection refused")
    show_error_dialog(window, err)

    assert captured["text"] == "ClamAVサービス（clamd）に接続できません"
    assert captured["informative"] == "エラーコード: E1001"


def test_error_dialog_detail_navigates_to_log_screen(qtbot, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)
    window.navigate_to("home")  # 事前にホーム画面にしておく

    detail_button_holder = {}

    def fake_exec(self):
        # addButtonで追加された「詳細を見る」ボタンをクリックされた状態にする
        for btn in self.buttons():
            if btn.text() == "詳細を見る":
                detail_button_holder["button"] = btn
        return 0

    def fake_clicked_button(self):
        return detail_button_holder.get("button")

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", fake_clicked_button)

    err = ClamdConnectionError()
    window.show_error(err)

    assert window.current_screen_key() == "log"
    log_screen = window.get_screen("log")
    assert log_screen.search_edit.text() == "E1001"


def test_header_status_pill_and_sidebar_footer_follow_status(qtbot):
    from gui.screens.home import AppStatus

    window = MainWindow()
    qtbot.addWidget(window)
    assert window.windowTitle() == "ClamQtPie"

    window.update_status_bar(AppStatus(clamd_running=True, db_version="28157", db_date="Sun Oct 18 2026"))
    assert "準備ができています" in window.status_pill.text()
    assert window.footer_db_label.text() == "定義DB: 28157"

    window.update_status_bar(AppStatus(clamd_running=False))
    assert "停止しています" in window.status_pill.text()
    assert window.footer_db_label.text() == "定義DB: -"


def test_hamburger_collapses_and_expands_sidebar(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    first = window.nav_list.item(0)
    assert first.text() == "ホーム"

    window.hamburger_button.click()
    assert window.sidebar_collapsed is True
    assert first.text() == ""
    assert first.toolTip() == "ホーム"

    window.hamburger_button.click()
    assert window.sidebar_collapsed is False
    assert first.text() == "ホーム"
