"""
未読の通知の一覧（トレイGUI。サービス化・Web API設計書 7.2節。段階 12-4b）

トレイのメニュー「未読の通知」から開く。選んだもの・すべてを既読にする。
タイトル・本文はサービスが制御文字を見える形にして保存しており、ここでは文字のまま表示する（plain）。
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.exceptions import AppError
from gui.error_dialog import show_error_dialog
from gui.plain_text import plain, plain_tooltip
from gui.screens.base import configure_table

#: 行に通知の ID を入れる場所
_ID_ROLE = Qt.ItemDataRole.UserRole


class NotificationListDialog(QDialog):
    def __init__(self, notifier, parent=None) -> None:
        super().__init__(parent)
        self._notifier = notifier
        self.setWindowTitle("未読の通知")
        self.resize(640, 360)
        layout = QVBoxLayout(self)
        self.summary_label = plain(QLabel(""))
        layout.addWidget(self.summary_label)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["日時", "通知", "内容"])
        configure_table(self.table)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox()
        self.ack_selected_button = QPushButton("選んだ通知を既読にする")
        self.ack_all_button = QPushButton("すべて既読にする")
        buttons.addButton(self.ack_selected_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.ack_all_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self.ack_selected_button.clicked.connect(lambda: self._ack(self._selected_ids()))
        self.ack_all_button.clicked.connect(lambda: self._ack(None))
        layout.addWidget(buttons)
        notifier.pending_changed.connect(lambda _count: self.reload())
        self.reload()

    def reload(self) -> None:
        items = self._notifier.pending
        self.table.setRowCount(0)
        for item in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [_local_time(item.get("created_at", "")), item.get("title", ""), item.get("body", "")]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(plain_tooltip(value))
                self.table.setItem(row, col, cell)
            self.table.item(row, 0).setData(_ID_ROLE, item["id"])
        self.summary_label.setText(f"未読の通知: {len(items)} 件" if items else "未読の通知はありません。")
        self.ack_selected_button.setEnabled(bool(items))
        self.ack_all_button.setEnabled(bool(items))

    def _selected_ids(self) -> list[int]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [self.table.item(row, 0).data(_ID_ROLE) for row in rows]

    def _ack(self, ids: list[int] | None) -> None:
        if ids is not None and not ids:
            return
        try:
            self._notifier.ack(ids)
        except AppError as exc:
            show_error_dialog(self, exc)
        self.reload()


def _local_time(value: str) -> str:
    """保存した日時（UTC の ISO 形式）を、この PC の時刻で表示する"""
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return value
