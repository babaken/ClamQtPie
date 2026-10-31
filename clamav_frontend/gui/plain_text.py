"""
外から来る文字列（ファイル名・clamdの応答・ログのメッセージ等）を画面に出すときの補助

QLabel・QMessageBox・ツールチップは、既定（Qt.TextFormat.AutoText）ではHTMLらしい文字列をHTMLとして表示する。
Linuxのファイル名には「<」「>」を使えるため、ファイル名などはHTMLとして解釈させず、文字のまま表示する。
"""
from __future__ import annotations

from typing import TypeVar

from PySide6.QtGui import Qt  # convertFromPlainText（QtGui）を含むQtの名前空間

_W = TypeVar("_W")


def plain(widget: _W) -> _W:
    """QLabel・QMessageBoxの文字列を、常に文字のまま表示させる（ウィジェットをそのまま返す）"""
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def plain_tooltip(text: str) -> str:
    """ツールチップ用に、文字のまま表示されるHTMLへ変換する（ツールチップは表示形式を指定できないため）"""
    if not text:
        return ""
    return Qt.convertFromPlainText(text, Qt.WhiteSpaceMode.WhiteSpaceNoWrap)
