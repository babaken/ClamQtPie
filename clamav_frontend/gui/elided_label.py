"""
長い文字列を、ラベルの幅に合わせて途中を「…」に縮めて表示するラベル

スキャン中のファイルのパスのように長さの決まらない文字列を QLabel にそのまま置くと、ラベルの最小の幅が
文字列の幅になり、画面全体が広がって右が見えなくなる。このラベルは画面の幅を決めず、もらった幅で表示する。
全体はツールチップで読める。文字列は常に文字のまま表示する（HTML として解釈しない）。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

from gui.plain_text import plain_tooltip


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideMiddle) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._mode = mode
        self.setTextFormat(Qt.TextFormat.PlainText)
        # 横の大きさの希望を出さない（レイアウトがくれた幅に合わせる）
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802（Qt の名前に合わせる）
        self._full_text = text or ""
        self.setToolTip(plain_tooltip(self._full_text))
        self._refresh()

    def text(self) -> str:
        """縮める前の文字列を返す"""
        return self._full_text

    def shown_text(self) -> str:
        """画面に出している（縮めた後の）文字列"""
        return super().text()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, super().sizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        width = max(self.contentsRect().width(), 0)
        shown = self.fontMetrics().elidedText(self._full_text, self._mode, width) if self._full_text else ""
        if shown != super().text():
            super().setText(shown)
