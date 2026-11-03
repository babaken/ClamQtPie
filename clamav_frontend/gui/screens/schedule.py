"""
スケジュール画面

参照: 画面設計書4章
Phase6: SchedulerServiceとの結線を追加
Phase 11: スケジュール・種別の人間可読表示、次回実行日時、最終実行結果（成功／失敗／検知あり）の表示と
          ログ画面への遷移、有効/無効の切替、「今すぐ実行」のバックグラウンド実行（実行中に画面が固まらない）を追加
段階 12-4c（トレイGUI）: 追加・編集・削除・有効無効の切り替えを画面の中にためておき、［変更を保存］で 1 回の昇格に
          まとめる（bind_services の batch。gui/pending_edits.py）。編集画面に「スリープ中でも起こして実行する」を出す
段階 12-4e: 従来のアプリ（その場でスケジュールを変える）の経路を除いた
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

from api_client.admin_ops import Operation
from core.exceptions import AppError, ElevationCancelledError
from gui.dialogs.job_edit_dialog import SCAN_TYPE_LABELS, JobEditDialog
from gui.error_dialog import show_error_dialog
from gui.pending_edits import MARK_UNSAVED, PendingEdits, SaveBar, failure_text
from gui.screens.base import BaseScreen, wrap_layout

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

        # 変更の保存
        self.save_card = self.begin_card("変更の保存")
        self.save_bar = SaveBar(self.save_changes, self.discard_changes)
        self.add_layout_widget(wrap_layout(self.save_bar.layout))
        self._batch = None
        self._pending = PendingEdits()

        self._scheduler_service = None
        self._job_runner: _JobRunner | None = None
        self._on_show_log = None

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(self, scheduler_service, batch, on_show_log=None) -> None:
        """batch(操作の一覧): ためておいた変更を、［変更を保存］でまとめて送る（api_client.elevation）
        on_show_log: 最終実行結果のダブルクリック時に呼ばれる（ジョブ名でログ画面を絞り込む）"""
        self._batch = batch
        self._scheduler_service = scheduler_service
        self._on_show_log = on_show_log
        self._job_runner = _JobRunner(scheduler_service)
        self._job_runner.finished.connect(self._on_run_finished)
        self.reload()

    def reload(self) -> None:
        if self._scheduler_service is None:
            return
        records = self._scheduler_service.list_all()
        pending = self._pending
        rows = []
        for record in records:
            shown = pending.updated.get(record.id, {})
            rows.append((record.id, shown.get("name", record.name), shown.get("target", record.target),
                         shown.get("scan_type", record.scan_type), shown.get("cron", record.cron),
                         pending.state_text(record.id, record.enabled), record.next_run, record.last_run))
        for i, item in enumerate(pending.added):
            rows.append((-(i + 1), item["name"], item["target"], item["scan_type"], item["cron"],
                         pending.state_text(-(i + 1), True), "", ""))
        self.job_table.setRowCount(0)
        for key, name, target, scan_type, cron, state, next_run, last_run in rows:
            row = self.job_table.rowCount()
            self.job_table.insertRow(row)
            values = [
                (name, target or ""),
                (SCAN_TYPE_LABELS.get(scan_type, scan_type), ""),
                (describe_cron(cron), f"Cron式: {cron}"),
                (state, ""),
                (_local_datetime(next_run) if next_run else "-", ""),
                (describe_last_run(last_run) if key > 0 else MARK_UNSAVED, "ダブルクリックでログを表示"),
            ]
            for col, (value, tooltip) in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(_ID_ROLE, key)
                if tooltip:
                    item.setToolTip(tooltip)
                self.job_table.setItem(row, col, item)
        self.save_bar.show_count(len(pending))

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
        dialog = JobEditDialog(self, show_wake=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._pending.add(dialog.result_values())
        self.reload()

    def _on_edit_job_clicked(self) -> None:
        if self._scheduler_service is not None:
            self._edit_pending()

    def _on_delete_job_clicked(self) -> None:
        key = self._selected_id()
        if key is not None:
            self._pending.remove(key)  # 保存するまでは取り消せるため、ここでは確かめない
            self.reload()

    def _on_toggle_job_clicked(self) -> None:
        record = self._selected_record()
        if record is None or self._scheduler_service is None:
            return
        self._pending.toggle(record.id, record.enabled)
        self.reload()

    def _on_run_now_clicked(self) -> None:
        record = self._selected_record()
        if record is None or self._job_runner is None:
            return
        if record.id in self._pending.updated or record.id in self._pending.removed:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(self, "今すぐ実行", "このジョブには保存していない変更があります。先に［変更を保存］してください。")
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

    # -- 変更の保存 ----------------------------------------------------------------
    def _edit_pending(self) -> None:
        key = self._selected_id()
        if key is None:
            return
        if key < 0:
            initial = dict(self._pending.added[-key - 1])
        else:
            record = next((r for r in self._scheduler_service.list_all() if r.id == key), None)
            if record is None or key in self._pending.removed:
                return
            initial = self._pending.updated.get(key) or {
                "name": record.name, "scan_type": record.scan_type, "target": record.target, "cron": record.cron,
                "wake": bool(getattr(record, "wake", False))}
        dialog = JobEditDialog(self, initial=initial, show_wake=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._pending.update(key, dialog.result_values())
        self.reload()

    def has_unsaved_changes(self) -> bool:
        return len(self._pending) > 0

    def pending_operations(self) -> list[Operation]:
        """保存していない変更を、API の操作の一覧にする（削除 → 変更 → 有効無効 → 追加の順）"""
        def body(values: dict) -> dict:
            return {k: values.get(k, "" if k != "wake" else False) for k in ("name", "scan_type", "cron", "target", "wake")}

        ops = [Operation("DELETE", f"/schedules/{key}") for key in sorted(self._pending.removed)]
        ops += [Operation("PUT", f"/schedules/{key}", body(values)) for key, values in sorted(self._pending.updated.items())]
        ops += [Operation("PATCH", f"/schedules/{key}", {"enabled": enabled})
                for key, enabled in sorted(self._pending.enabled.items())]
        ops += [Operation("POST", "/schedules", body(values)) for values in self._pending.added]
        return ops

    def save_changes(self) -> bool:
        """［変更を保存］: 1 回の昇格でまとめて保存する。保存できた（または変更が無い）とき True"""
        if self._batch is None:
            return True  # まだ結線していない
        ops = self.pending_operations()
        if not ops:
            return True
        try:
            self._batch(ops)
        except ElevationCancelledError:
            return False  # 取り消した: 変更はそのまま残す
        except AppError as exc:
            self._pending.clear()
            self.reload()
            self.save_bar.show_result(failure_text(exc, len(ops)) + "一覧はサービスの今の内容です。", ok=False)
            show_error_dialog(self, exc)
            return False
        self._pending.clear()
        self.reload()
        self.save_bar.show_result(f"{len(ops)} 件の変更を保存しました。", ok=True)
        return True

    def discard_changes(self) -> None:
        self._pending.clear()
        self.reload()
        self.save_bar.show_result("保存していない変更を取り消しました。", ok=True)
