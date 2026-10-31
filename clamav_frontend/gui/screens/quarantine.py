"""
隔離画面

参照: 画面設計書5章
Phase4: QuarantineServiceとの結線を追加
Phase 11: 期間フィルタ、SHA256の省略表示（ツールチップで全体）、サイズの単位表示、詳細表示、
          複数選択による一括削除、復元先に同名ファイルがある場合の上書き／別名指定を追加
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from core.exceptions import AppError, QuarantineWriteError
from gui.error_dialog import show_confirm_dialog, show_error_dialog
from gui.plain_text import plain, plain_tooltip
from gui.screens.base import BaseScreen, wrap_layout

STATUS_QUARANTINED = "隔離中"
STATUS_RESTORED = "復元済"
STATUS_DELETED = "削除済"
_SHA256_SHORT_LEN = 12


def status_of(record) -> str:
    return STATUS_RESTORED if record.restored else (STATUS_DELETED if record.deleted else STATUS_QUARANTINED)


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class QuarantineScreen(BaseScreen):
    screen_title = "隔離フォルダー"
    screen_subtitle = "隔離した不正なファイルを SHA-256 による整合性検証付きで管理します。"

    def build_content(self) -> None:
        # 1. フィルタ／検索バー（期間、ウイルス名・元パスの部分一致）
        self.begin_card()
        filter_layout = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("元パス・ウイルス名で検索")
        self.search_edit.textChanged.connect(self._on_filter_changed)
        self.period_check = QCheckBox("期間")
        self.date_from_edit = QDateEdit(QDate.currentDate().addDays(-30))
        self.date_to_edit = QDateEdit(QDate.currentDate())
        for w in (self.date_from_edit, self.date_to_edit):
            w.setCalendarPopup(True)
            w.dateChanged.connect(self._on_filter_changed)
        self.period_check.toggled.connect(self._on_filter_changed)
        filter_layout.addWidget(self.search_edit, stretch=1)
        filter_layout.addWidget(self.period_check)
        filter_layout.addWidget(self.date_from_edit)
        filter_layout.addWidget(QLabel("～"))
        filter_layout.addWidget(self.date_to_edit)
        self._add_hlayout(filter_layout)

        # 2. 隔離一覧テーブル（複数選択可: 一括削除用）
        self.begin_card("検出されたアイテム")
        self.quarantine_table = QTableWidget(0, 6)
        self.quarantine_table.setHorizontalHeaderLabels(
            ["元ファイルパス", "ウイルス名", "検知日時", "サイズ", "SHA256", "状態"]
        )
        self.quarantine_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.quarantine_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.quarantine_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.add_layout_widget(self.quarantine_table)

        action_layout = QHBoxLayout()
        self.restore_button = QPushButton("復元")
        self.delete_button = QPushButton("完全削除（複数選択可）")
        self.detail_button = QPushButton("詳細表示")
        self.restore_button.clicked.connect(self._on_restore_clicked)
        self.delete_button.clicked.connect(self._on_delete_clicked)
        self.detail_button.clicked.connect(self._on_detail_clicked)
        for btn in (self.restore_button, self.delete_button, self.detail_button):
            action_layout.addWidget(btn)
        self._add_hlayout(action_layout)

        self._quarantine_service = None
        self._records = []

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(self, quarantine_service) -> None:
        self._quarantine_service = quarantine_service
        self.reload()

    def reload(self) -> None:
        """一覧を再読込して表示する（QT-I01: 検知→隔離→一覧表示の確認対象）"""
        if self._quarantine_service is None:
            return
        keyword = self.search_edit.text().strip()
        records = self._quarantine_service.list_all()
        if keyword:
            records = [r for r in records if keyword in r.original_path or keyword in r.virus_name]
        if self.period_check.isChecked():
            date_from = self.date_from_edit.date().toString("yyyy-MM-dd")
            date_to = self.date_to_edit.date().toString("yyyy-MM-dd")
            records = [r for r in records if date_from <= _local_date(r.detected_at) <= date_to]
        self._records = records
        self._render_table(records)

    def _render_table(self, records) -> None:
        self.quarantine_table.setRowCount(0)
        for record in records:
            row = self.quarantine_table.rowCount()
            self.quarantine_table.insertRow(row)
            values = [
                (record.original_path, record.original_path),
                (record.virus_name, record.virus_name),
                (_local_datetime(record.detected_at), record.detected_at),
                (format_size(record.size), f"{record.size:,} バイト"),
                (record.sha256[:_SHA256_SHORT_LEN] + "…", record.sha256),  # 画面設計書5章: 省略表示＋ホバーで全体
                (status_of(record), ""),
            ]
            for col, (text, tooltip) in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(_ID_ROLE, record.id)
                if tooltip:
                    item.setToolTip(plain_tooltip(tooltip))
                self.quarantine_table.setItem(row, col, item)

    def _on_filter_changed(self, *_args) -> None:
        self.reload()

    def _selected_records(self) -> list:
        rows = sorted({idx.row() for idx in self.quarantine_table.selectedIndexes()})
        by_id = {r.id: r for r in self._records}
        result = []
        for row in rows:
            item = self.quarantine_table.item(row, 0)
            if item is not None and item.data(_ID_ROLE) in by_id:
                result.append(by_id[item.data(_ID_ROLE)])
        return result

    def _selected_id(self) -> int | None:
        records = self._selected_records()
        return records[0].id if records else None

    # -- 復元 -----------------------------------------------------
    def _on_restore_clicked(self) -> None:
        if self._quarantine_service is None:
            return
        records = self._selected_records()
        if not records:
            return
        record = records[0]
        if status_of(record) != STATUS_QUARANTINED:
            QMessageBox.information(self, "復元", f"このファイルは{status_of(record)}のため復元できません。")
            return
        if not show_confirm_dialog(self, "復元の確認", f"次のファイルを元の場所へ復元しますか？\n{record.original_path}"):
            return

        dest_path, overwrite = None, False
        if Path(record.original_path).exists():
            # 画面設計書5章: 復元先に同名ファイルがある場合は上書き確認 or 別名指定
            choice = self._ask_restore_conflict(record.original_path)
            if choice == "cancel":
                return
            if choice == "overwrite":
                overwrite = True
            else:
                dest_path, _ = QFileDialog.getSaveFileName(self, "復元先のファイル名を指定", record.original_path)
                if not dest_path:
                    return
                overwrite = True  # 保存ダイアログ側で上書き確認済み

        try:
            self._quarantine_service.restore(record.id, dest_path=dest_path, overwrite=overwrite)
            self.reload()
        except AppError as exc:
            show_error_dialog(self, exc)
        except Exception as exc:  # pragma: no cover - 想定外例外の保険
            show_error_dialog(self, QuarantineWriteError(detail=str(exc)))

    def _ask_restore_conflict(self, path: str) -> str:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("復元先に同名のファイルがあります")
        plain(box).setText(f"復元先に同じ名前のファイルがあります。\n{path}")  # ファイル名を文字のまま表示する
        overwrite_button = box.addButton("上書き", QMessageBox.ButtonRole.DestructiveRole)
        rename_button = box.addButton("別名で保存", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is overwrite_button:
            return "overwrite"
        if clicked is rename_button:
            return "rename"
        return "cancel"

    # -- 完全削除（一括） ----------------------------------------------------
    def _on_delete_clicked(self) -> None:
        if self._quarantine_service is None:
            return
        records = [r for r in self._selected_records() if status_of(r) == STATUS_QUARANTINED]
        if not records:
            return
        if not show_confirm_dialog(
            self,
            "完全削除の確認",
            f"選択した{len(records)}件のファイルを完全に削除します。元に戻せません。よろしいですか？",
        ):
            return
        for record in records:
            try:
                self._quarantine_service.delete_permanently(record.id)
            except AppError as exc:
                show_error_dialog(self, exc)
            except Exception as exc:  # pragma: no cover
                show_error_dialog(self, QuarantineWriteError(detail=str(exc)))
        self.reload()

    # -- 詳細表示 -----------------------------------------------------
    def _on_detail_clicked(self) -> None:
        records = self._selected_records()
        if not records:
            return
        r = records[0]
        box = plain(QMessageBox(QMessageBox.Icon.Information, "隔離ファイルの詳細", "", parent=self))
        box.setText(
            "\n".join(
                [
                    f"元ファイルパス: {r.original_path}",
                    f"ウイルス名: {r.virus_name}",
                    f"検知日時: {_local_datetime(r.detected_at)}",
                    f"サイズ: {format_size(r.size)}（{r.size:,} バイト）",
                    f"SHA256: {r.sha256}",
                    f"状態: {status_of(r)}",
                    f"隔離ファイル名（UUID）: {r.uuid}",
                ]
            ),
        )
        box.exec()

    def _add_hlayout(self, layout: QHBoxLayout) -> None:
        self.add_layout_widget(wrap_layout(layout))


def _local_datetime(iso: str) -> str:
    """DBのUTC ISO8601を表示用のローカル時刻へ変換する"""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def _local_date(iso: str) -> str:
    return _local_datetime(iso)[:10]


_ID_ROLE = 0x0101
