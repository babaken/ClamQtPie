"""
メイン画面を最小の幅にしても、各画面の中身が表示の幅に収まる（README_DEV §108。#85）

狭いと、折り返さない長いラベル・横 1 列のボタンで画面の右がはみ出して切れ、横のスクロールバーも無かったため
押せないボタンがあった。Linux のデスクトップは Windows より文字が大きいことが多いため、大きめの文字でも確かめる。
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea, QWidget

from gui.flow_layout import FlowLayout
from gui.main_window import MainWindow
from gui.screens.home import AppStatus


@pytest.fixture
def larger_font(qapp):
    original = QApplication.font()
    font = QApplication.font()
    font.setPointSizeF(11.0)
    QApplication.setFont(font)
    yield
    QApplication.setFont(original)


def test_every_screen_fits_the_minimum_window_width(qtbot, larger_font):
    window = MainWindow()
    qtbot.addWidget(window)
    # 長い値（定義DB の版と日時）が入った状態で確かめる
    window.update_status_bar(AppStatus(clamd_running=True, db_version="28182", db_date="Fri Nov 13 15:24:28 2026"))
    home = window.get_screen("home")
    home.db_version_card.set_value("28182 / Fri Nov 13 15:24:28 2026")
    home.clamd_status_card.set_value("稼働中")
    window.resize(window.minimumWidth(), window.minimumHeight())
    window.show()

    too_wide = []
    for key in ("home", "scan", "update", "log", "quarantine", "engine", "realtime", "schedule", "diagnostics",
                "settings"):
        screen = window.get_screen(key)
        window.stacked_widget.setCurrentWidget(screen)
        QApplication.processEvents()
        scroll = screen.findChild(QScrollArea, "screenScroll")
        needed = scroll.widget().minimumSizeHint().width()
        if needed > scroll.viewport().width():
            too_wide.append((key, needed, scroll.viewport().width()))
    assert too_wide == []


def _buttons(parent, count):
    return [QPushButton(f"ボタン {i} の長めの名前", parent) for i in range(count)]


def test_flow_layout_wraps_to_the_next_row_without_overlap(qtbot):
    container = QWidget()
    qtbot.addWidget(container)
    layout = FlowLayout(container)
    buttons = _buttons(container, 4)
    for button in buttons:
        layout.addWidget(button)
    widest = max(b.sizeHint().width() for b in buttons)

    # 最小の幅は、いちばん広い 1 つの幅
    assert layout.minimumSize().width() == max(b.minimumSizeHint().width() for b in buttons)

    # 2 つ分ほどの幅では、2 行以上に折り返し、重ならない
    width = widest * 2 + layout.spacing() + 4
    layout.setGeometry(container.rect().adjusted(0, 0, width - container.width(), 0))
    rects = [b.geometry() for b in buttons]
    assert len({r.y() for r in rects}) >= 2
    for i, a in enumerate(rects):
        assert a.right() < width
        for b in rects[i + 1:]:
            assert not a.intersects(b)
    assert layout.heightForWidth(width) > buttons[0].sizeHint().height()

    # 広いときは 1 行
    wide = sum(b.sizeHint().width() for b in buttons) + layout.spacing() * 3 + 4
    assert layout.heightForWidth(wide) == max(b.sizeHint().height() for b in buttons)


def test_home_action_cards_share_the_width_equally(qtbot):
    # 操作カードの左右の列は半分ずつ（見出しが折り返せるようになって、右の列が細くなった。PR #86 の確認で指摘）
    from PySide6.QtWidgets import QGridLayout

    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    QApplication.processEvents()
    grid = window.get_screen("home").findChildren(QGridLayout)[0]
    left, right = grid.cellRect(0, 0).width(), grid.cellRect(0, 1).width()
    assert abs(left - right) <= 2
    # 文字の幅しだいで偏らないよう、伸び方を左右同じにしてあること（検査用の表示の字体では偏りが出ないため）
    assert grid.columnStretch(0) == grid.columnStretch(1) == 1


def test_status_card_prefers_one_line_when_there_is_room(qtbot):
    # 値が 1 行に入る幅があれば、1 行の高さにする（折り返す QLabel の既定の大きさでは 2 行分の高さになり、行の間が空いた）
    window = MainWindow()
    qtbot.addWidget(window)
    card = window.get_screen("home").db_version_card
    card.set_value("28182 / Fri Nov 13 15:24:28 2026")
    hint = card.sizeHint()
    assert hint.height() < 2 * card.value_label.fontMetrics().height()


def test_flow_layout_line_spacing_is_separate_from_item_spacing(qtbot):
    # 折り返した行の間は、横の間隔と別に決められる（ホームの枠で行の間が空きすぎた。PR #86 の確認で指摘）
    container = QWidget()
    qtbot.addWidget(container)
    layout = FlowLayout(container, spacing=18, line_spacing=4)
    buttons = _buttons(container, 2)
    for button in buttons:
        layout.addWidget(button)
    width = max(b.sizeHint().width() for b in buttons) + 4  # 1 つずつしか入らない幅
    layout.setGeometry(container.rect().adjusted(0, 0, width - container.width(), 0))
    first, second = (b.geometry() for b in buttons)
    assert second.y() - (first.y() + first.height()) == 4


_LONG_PATH = "C:/Users/someone/AppData/Local/Temp/abcd1234/Microsoft.DiagnosticsHub.Runtime.Resources." + "0" * 120 + ".vsix"


def test_scan_screen_does_not_widen_while_scanning(qtbot, larger_font):
    """スキャン中の長いパス・終わったときの長い状態の文でも、スキャン画面は最小の幅に収まる（v0.10.1 の確認で見つけた）"""
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(window.minimumWidth(), window.minimumHeight())
    window.show()
    screen = window.get_screen("scan")
    window.stacked_widget.setCurrentWidget(screen)
    scroll = screen.findChild(QScrollArea, "screenScroll")

    screen.current_file_label.setText(_LONG_PATH)
    screen.state_label.setText("状態: 完了（検査できなかったファイルが 4 件あります。スキャン結果の一覧を確認してください）")
    screen._add_result_row(_LONG_PATH, "ERROR", "Bad file descriptor.", "2026-11-14T07:00:27+09:00")
    QApplication.processEvents()

    assert scroll.widget().minimumSizeHint().width() <= scroll.viewport().width()
    assert not scroll.horizontalScrollBar().isVisible()
    # パスは途中を「…」にして見せ、全体はツールチップと text() で読める
    label = screen.current_file_label
    assert label.text() == _LONG_PATH
    assert label.shown_text() != _LONG_PATH and "…" in label.shown_text()
    assert label.shown_text().startswith("C:/Users") and label.shown_text().endswith(".vsix")
    assert "DiagnosticsHub" in label.toolTip()


def test_path_column_takes_the_spare_width(qtbot):
    """スキャン結果・隔離の表は、パスの列に残りの幅を回す（100 ピクセルほどで「C:...」としか見えなかった）"""
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1200, 800)
    window.show()
    for key, attr in (("scan", "result_table"), ("quarantine", "quarantine_table")):
        screen = window.get_screen(key)
        window.stacked_widget.setCurrentWidget(screen)
        QApplication.processEvents()
        table = getattr(screen, attr)
        widths = [table.columnWidth(c) for c in range(table.columnCount())]
        assert widths[0] == max(widths), (key, widths)
        assert widths[0] > 200, (key, widths)
        # 1 行で、途中を「…」にする（折り返すと「C:...」しか見えなかった。v0.10.1 の Windows 実機の確認で指摘）
        assert not table.wordWrap()
        assert table.textElideMode() == Qt.TextElideMode.ElideMiddle
