"""
外から来る文字列（ファイル名・clamdの応答・ログ）を画面に出すときの表示（gui/plain_text.py と各画面）のテスト

Linuxのファイル名には「<」「>」を使えるため、HTMLとして解釈させず、文字のまま表示すること。
"""
from unittest.mock import MagicMock

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QLabel

from gui.plain_text import plain, plain_tooltip
from gui.screens.home import HomeScreen
from gui.screens.scan import ScanScreen

_NAME_WITH_MARKUP = "/home/u/<b>report</b>&<i>x</i>.pdf"


def _rendered_text(html: str) -> str:
    document = QTextDocument()
    document.setHtml(html)
    return document.toPlainText()


def test_plain_label_shows_markup_as_text(qtbot):
    label = plain(QLabel())
    qtbot.addWidget(label)
    label.setText(_NAME_WITH_MARKUP)
    assert label.textFormat() == Qt.TextFormat.PlainText


def test_plain_tooltip_shows_markup_as_text():
    assert _rendered_text(plain_tooltip(_NAME_WITH_MARKUP)) == _NAME_WITH_MARKUP
    assert _rendered_text(plain_tooltip("1行目\n2行目")) == "1行目\n2行目"
    assert plain_tooltip("") == ""


def test_scan_screen_shows_current_file_as_text(qtbot):
    screen = ScanScreen()
    qtbot.addWidget(screen)
    assert screen.current_file_label.textFormat() == Qt.TextFormat.PlainText
    assert screen.state_label.textFormat() == Qt.TextFormat.PlainText


def test_home_cards_show_clamd_values_as_text(qtbot):
    screen = HomeScreen()
    qtbot.addWidget(screen)
    clamav_service = MagicMock()
    clamav_service.ping.return_value = True
    clamav_service.get_version.return_value = ("<b>ClamAV</b>", "1", "<i>date</i>")
    realtime_manager = MagicMock()
    realtime_manager.is_running = False
    screen.bind_services(clamav_service, realtime_manager, MagicMock(), MagicMock(), MagicMock(), stale_days=lambda: 3)

    value_label = screen.db_version_card.findChild(QLabel, "value_label")
    assert value_label.textFormat() == Qt.TextFormat.PlainText
    assert "<i>date</i>" in value_label.text()
