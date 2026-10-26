"""
スケジュール画面

参照: 画面設計書4章
Phase6: SchedulerServiceとの結線を追加
Phase 11: スケジュール・種別の人間可読表示、次回実行日時、最終実行結果（成功／失敗／検知あり）の表示と
          ログ画面への遷移、有効/無効の切替、「今すぐ実行」のバックグラウンド実行（実行中に画面が固まらない）を追加
"""
from __future__ import annotations

import re
import threading
from datetime import datetime

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from core.exceptions import AppError
from gui.dialogs.job_edit_dialog import SCAN_TYPE_LABELS, JobEditDialog
from gui.error_dialog import show_confirm_dialog, show_error_dialog
from gui.screens.base import BaseScreen

_ID_ROLE = 0x0103
_WEEKDAYS = {"0": "日", "1": "月", "2": "火", "3": "水", "4": "木", "5": "金", "6": "土", "7": "日"}
_COL_LAST_RUN = 5
_LAST_RUN_RE = re.compile(r"^(?P<time>\S+)(?: \((?P<note>.+)\))?$")


def describe_cron(cron: str) -> str:
    """cron式を「毎日 3:00」等の人間可読表現にする（画面設計書4章）。該当しない形はそのまま返す"""
    parts = cron.split()
    if len(parts) != 5 or not (parts[0].isdigit() and parts[1].isdigit()):
        return cron
    minute, hour, day, month, weekday = parts
    time_text = f"{int(hour)}:{int(minute):02d}"
    if day == "*" and month == "*" and weekday == "*":
        return f"毎日 {time_text}"
    if day == "*" and month == "*" and weekday in _WEEKDAYS:
        return f"毎週{_WEEKDAYS[weekday]}曜 {time_text}"
    if day.isdigit() and month == "*" and weekday == "*":
        return f"毎月{int(day)}日 {time_text}"
    return cron


def describe_last_run(last_run: str) -> str:
    """最終実行結果を「2026-10-19 12:00:00 成功／失敗／検知あり n件」の形にする"""
    if not last_run:
        return "未実行"
    m = _LAST_RUN_RE.match(last_run)
    if not m:
        return last_run
    return f"{_local_datetime(m.group('time'))} {m.group('note') or '成功'}"


def _local_datetime(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


class _JobRunner(QObject):
    """「今すぐ実行」をバックグラウンドで実行する（スキャンは長時間かかるためGUIスレッドで実行しない）"""

    finished = Signal(object)  # 失敗時はAppError、成功時はNone

    def __init__(self, scheduler_service) -> None:
        super().__init__()
        self._scheduler_service = scheduler_service

    def start(self, job_id: int) -> None:
        threading.Thread(target=self._run, args=(job_id,), daemon=True).start()

    def _run(self, job_id: int) -> None:
        try:
            self._scheduler_service.run_now(job_id)
            self.finished.emit(None)
        except AppError as exc:
            self.finished.emit(exc)


class ScheduleScreen(BaseScreen):
    screen_title = "スキャンの自動実行"
    screen_subtitle = "決まった時刻にスキャンや定義更新をバックグラウンドで自動実行するよう設定します。"

    def build_content(self) -> None:
        self.begin_card("スケジュール ジョブ")
        self.job_table = QTableWidget(0, 6)
        self.job_table.setHorizontalHeaderLabels(
            ["名前", "種別", "スケジュール", "有効", "次回実行日時", "最終実行結果"]
        )
        self.job_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.job_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.job_table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.add_layout_widget(self.job_table)

        action_layout = QHBoxLayout()
        self.add_job_button = QPushButton("新規ジョブ追加")
        self.edit_job_button = QPushButton("編集")
        self.delete_job_button = QPushButton("削除")
        self.toggle_job_button = QPushButton("有効/無効を切替")
        self.run_now_button = QPushButton("今すぐ実行")
        self.add_job_button.clicked.connect(self._on_add_job_clicked)
        self.edit_job_button.clicked.connect(self._on_edit_job_clicked)
        self.delete_job_button.clicked.connect(self._on_delete_job_clicked)
        self.toggle_job_button.clicked.connect(self._on_toggle_job_clicked)
        self.run_now_button.clicked.connect(self._on_run_now_clicked)
        for btn in (
            self.add_job_button,
            self.edit_job_button,
            self.delete_job_button,
            self.toggle_job_button,
            self.run_now_button,
        ):
            action_layout.addWidget(btn)

        from PySide6.QtWidgets import QFrame

        frame = QFrame()
        frame.setLayout(action_layout)
        self.add_layout_widget(frame)

        self._scheduler_service = None
        self._job_runner: _JobRunner | None = None
        self._on_show_log = None

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(self, scheduler_service, on_show_log=None) -> None:
        """on_show_log: 最終実行結果のダブルクリック時に呼ばれる（ジョブ名でログ画面を絞り込む）"""
        self._scheduler_service = scheduler_service
        self._on_show_log = on_show_log
        self._job_runner = _JobRunner(scheduler_service)
        self._job_runner.finished.connect(self._on_run_finished)
        self.reload()

    def reload(self) -> None:
        if self._scheduler_service is None:
            return
        records = self._scheduler_service.list_all()
        self.job_table.setRowCount(0)
        for record in records:
            row = self.job_table.rowCount()
            self.job_table.insertRow(row)
            values = [
                (record.name, record.target or ""),
                (SCAN_TYPE_LABELS.get(record.scan_type, record.scan_type), ""),
                (describe_cron(record.cron), f"Cron式: {record.cron}"),
                ("有効" if record.enabled else "無効", ""),
                (_local_datetime(record.next_run) if record.next_run else "-", ""),
                (describe_last_run(record.last_run), "ダブルクリックでログを表示"),
            ]
            for col, (value, tooltip) in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(_ID_ROLE, record.id)
                if tooltip:
                    item.setToolTip(tooltip)
                self.job_table.setItem(row, col, item)

    def _selected_id(self) -> int | None:
        selected_rows = {idx.row() for idx in self.job_table.selectedIndexes()}
        if not selected_rows:
            return None
        row = next(iter(selected_rows))
        item = self.job_table.item(row, 0)
        return item.data(_ID_ROLE) if item else None

    def _selected_record(self):
        job_id = self._selected_id()
        if job_id is None or self._scheduler_service is None:
            return None
        matches = [r for r in self._scheduler_service.list_all() if r.id == job_id]
        return matches[0] if matches else None

    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        # 画面設計書4章: 最終実行結果のクリックでログ画面へフィルタ遷移
        if column == _COL_LAST_RUN and self._on_show_log is not None:
            self._on_show_log(self.job_table.item(row, 0).text())

    # -- ジョブCRUD -----------------------------------------------------
    def _on_add_job_clicked(self) -> None:
        if self._scheduler_service is None:
            return
        dialog = JobEditDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.result_values()
        try:
            self._scheduler_service.add_job(
                values["name"], values["scan_type"], values["cron"], target=values["target"]
            )
            self.reload()
        except AppError as exc:
            show_error_dialog(self, exc)

    def _on_edit_job_clicked(self) -> None:
        record = self._selected_record()
        if record is None or self._scheduler_service is None:
            return
        dialog = JobEditDialog(
            self,
            initial={
                "name": record.name,
                "scan_type": record.scan_type,
                "target": record.target,
                "cron": record.cron,
            },
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.result_values()
        try:
            self._scheduler_service.update_job(
                record.id, values["name"], values["scan_type"], values["cron"], target=values["target"]
            )
            self.reload()
        except AppError as exc:
            show_error_dialog(self, exc)

    def _on_delete_job_clicked(self) -> None:
        record = self._selected_record()
        if record is None or self._scheduler_service is None:
            return
        if not show_confirm_dialog(self, "削除の確認", f"ジョブ「{record.name}」を削除しますか？"):
            return
        self._scheduler_service.remove_job(record.id)
        self.reload()

    def _on_toggle_job_clicked(self) -> None:
        record = self._selected_record()
        if record is None or self._scheduler_service is None:
            return
        self._scheduler_service.set_enabled(record.id, not record.enabled)
        self.reload()

    def _on_run_now_clicked(self) -> None:
        record = self._selected_record()
        if record is None or self._job_runner is None:
            return
        self._job_runner.start(record.id)
        # 完了するとreload()で実際の結果に置き換わる
        for row in range(self.job_table.rowCount()):
            if self.job_table.item(row, 0).data(_ID_ROLE) == record.id:
                self.job_table.item(row, _COL_LAST_RUN).setText("実行中...")

    def _on_run_finished(self, error) -> None:
        self.reload()
        if error is not None:
            show_error_dialog(self, error)
