"""
画面共通基底クラス

各画面は BaseScreen を継承する。Phase4以降で各画面固有のロジックを
差し込みやすくするため、共通のタイトルラベル配置のみを基底側で行う。
画面レイアウト刷新: 画面タイトル＋説明文（screen_subtitle）を上部に置き、内容はカード
（begin_card()〜end_card()の間にadd_layout_widget()したもの）にまとめる。
内容が収まらない場合は画面全体を縦にスクロールする。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QTableWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

#: 日時列の既定幅（「2026-10-19 12:00:00」が省略されずに表示される幅）
_DATETIME_COLUMN_WIDTH = 165
#: 画面の内容の最大幅（横長のウィンドウで表やボタンが間延びしないように左寄せで止める）
_CONTENT_MAX_WIDTH = 1100
_TABLE_MIN_HEIGHT = 150


def configure_table(table: QTableWidget) -> None:
    """一覧テーブルの共通設定: 読み取り専用・行単位選択・最終列を幅いっぱいに伸ばす・日時列の幅を確保"""
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    # 画面がスクロールする高さでも表が数行は見えるようにする
    table.setMinimumHeight(_TABLE_MIN_HEIGHT)
    if table.columnCount() == 2:
        # 「パス／値」＋「有効」の2列表は、短い「有効」列ではなくパス列を広げる
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    else:
        table.horizontalHeader().setStretchLastSection(True)
    for col in range(table.columnCount()):
        header = table.horizontalHeaderItem(col)
        if header is not None and "日時" in header.text():
            table.setColumnWidth(col, _DATETIME_COLUMN_WIDTH)


def wrap_layout(layout) -> QWidget:
    """レイアウトを余白なしのウィジェットに包む（カードの中に横並びのボタン列等を置くため）"""
    widget = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    widget.setLayout(layout)
    return widget


def make_card(title: str = "", subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
    """角丸のカード（QFrame#card）と、その中身を並べるレイアウト"""
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(18, 14, 18, 16)
    layout.setSpacing(10)
    if title:
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        layout.addWidget(title_label)
    if subtitle:
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("cardText")
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)
    return card, layout


class BaseScreen(QWidget):
    #: 画面設計書の章タイトルと対応させる（サブクラスで上書き）
    screen_title: str = ""
    #: タイトルの下に表示する画面の説明（サブクラスで上書き）
    screen_subtitle: str = ""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("screenScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        viewport = QWidget()
        viewport.setObjectName("screenBody")
        viewport_layout = QHBoxLayout(viewport)
        viewport_layout.setContentsMargins(40, 28, 40, 24)
        body = QWidget()
        body.setObjectName("screenBody")
        body.setMaximumWidth(_CONTENT_MAX_WIDTH)
        viewport_layout.addWidget(body, stretch=1)
        viewport_layout.addStretch(0)
        scroll.setWidget(viewport)

        self._root_layout = QVBoxLayout(body)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(16)
        self._target_layout: QVBoxLayout = self._root_layout
        self._current_card: QFrame | None = None
        self._has_expanding_widget = False

        if self.screen_title:
            header = QVBoxLayout()
            header.setSpacing(2)
            title_label = QLabel(self.screen_title)
            title_label.setObjectName("screenTitle")
            header.addWidget(title_label)
            if self.screen_subtitle:
                subtitle_label = QLabel(self.screen_subtitle)
                subtitle_label.setObjectName("screenSubtitle")
                subtitle_label.setWordWrap(True)
                header.addWidget(subtitle_label)
            self._root_layout.addLayout(header)

        self.build_content()
        self.end_card()
        if not self._has_expanding_widget:
            self._root_layout.addStretch(1)

    def build_content(self) -> None:
        """サブクラスでオーバーライドし、self._root_layoutへウィジェットを追加する。
        Phase3時点ではプレースホルダのみ表示する。
        """
        placeholder = QLabel(f"（{self.screen_title}は今後のフェーズで実装します）")
        placeholder.setObjectName("mutedText")
        self._root_layout.addWidget(placeholder)

    # -- カード ---------------------------------------------------------
    def begin_card(self, title: str = "", subtitle: str = "") -> QFrame:
        """以降のadd_layout_widget()を新しいカードの中へ並べる（前のカードは閉じる）"""
        self.end_card()
        card, layout = make_card(title, subtitle)
        self._root_layout.addWidget(card)
        self._current_card = card
        self._target_layout = layout
        return card

    def end_card(self) -> None:
        """以降のadd_layout_widget()をカードの外（画面直下）へ並べる"""
        self._current_card = None
        self._target_layout = self._root_layout

    def add_layout_widget(self, widget: QWidget) -> None:
        # 一覧・ログ表示は残りの高さいっぱいに広げる（固定の余白で表が数行しか見えなくならないように）
        if isinstance(widget, QTableWidget):
            configure_table(widget)
        if isinstance(widget, (QTableWidget, QTextEdit, QTabWidget)):
            self._has_expanding_widget = True
            self._target_layout.addWidget(widget, stretch=1)
            if self._current_card is not None:
                self._root_layout.setStretchFactor(self._current_card, 1)
            return
        self._target_layout.addWidget(widget)
