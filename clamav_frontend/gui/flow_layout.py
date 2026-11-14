"""
幅が足りないときに次の行へ折り返して並べるレイアウト（README_DEV §108。#85）

ボタン・状態の小さなカードを横 1 列に並べた QHBoxLayout は、中身の幅の合計より狭くできない。メイン画面を最小の幅に
すると画面の右がはみ出して切れたため、横に並べるものはこのレイアウトで並べ、入り切らない分を次の行へ送る。
最小の幅は、いちばん広い 1 つの幅（＋余白）になる。
"""
from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 8, line_spacing: int | None = None) -> None:
        """spacing は横に並べる間隔、line_spacing は折り返した行の間（省くと spacing と同じ）"""
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self._line_spacing = spacing if line_spacing is None else line_spacing
        self.setContentsMargins(0, 0, 0, 0)

    # -- QLayout -----------------------------------------------------------
    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qtのオーバーライド
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def spacing(self) -> int:
        return self._spacing

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        # 望ましい大きさは 1 行に並べたとき
        width = sum(item.sizeHint().width() for item in self._visible())
        width += self._spacing * max(len(self._visible()) - 1, 0)
        height = max((item.sizeHint().height() for item in self._visible()), default=0)
        return QSize(width, height) + self._margin_size()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._visible():
            size = size.expandedTo(item.minimumSize())
        return size + self._margin_size()

    # -- 内部 ----------------------------------------------------------------
    def _visible(self) -> list[QLayoutItem]:
        return [item for item in self._items if not item.isEmpty()]

    def _margin_size(self) -> QSize:
        m: QMargins = self.contentsMargins()
        return QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QRect, apply: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_height = area.x(), area.y(), 0
        for item in self._visible():
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:
                x = area.x()
                y += line_height + self._line_spacing
                line_height = 0
            # 1 つで行の幅を超えるもの（折り返す文字を含む）は、行の幅に縮めて、その幅で要る高さにする
            width = max(min(hint.width(), area.width()), item.minimumSize().width())
            height = item.heightForWidth(width) if item.hasHeightForWidth() else hint.height()
            if height < 0:
                height = hint.height()
            if apply:
                item.setGeometry(QRect(QPoint(x, y), QSize(width, height)))
            x += width + self._spacing
            line_height = max(line_height, height)
        return y + line_height - rect.y() + m.bottom()
