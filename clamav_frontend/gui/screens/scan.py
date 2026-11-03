"""
スキャン画面

参照: 画面設計書2章
Phase4: ScanDispatcher / QuarantineService との結線を追加
Phase 11: クイック/フルスキャンの対象確定（core/scan_targets.py）、ファイル追加・対象削除、
          対象パスの存在確認、エラー結果の表示、「無視する」、状態表示（待機中/スキャン中/完了/中断）を追加
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from core import scan_targets
from core.clamav_service import (
    ERROR_REASON_OTHER,
    ERROR_REASON_PERMISSION,
    ERROR_REASON_SIZE_LIMIT,
    ERROR_REASON_VANISHED,
    error_reason,
)
from core.exceptions import AppError
from core.logger import CATEGORY_SCAN, log_event
from core.scan_history_service import MAX_ERRORS
from core.text_safety import visible_controls
from gui.error_dialog import show_confirm_dialog, show_error_dialog
from gui.plain_text import plain, plain_tooltip
from gui.screens.base import BaseScreen, wrap_layout
from gui.theme import set_variant
from gui.time_text import local_time_text

logger = logging.getLogger("clamav_frontend")

_VIEW_UPDATE_INTERVAL_MS = 200

#: 判定列の表示（画面設計書2章: 正常・検知・エラー）
_STATUS_LABELS = {"FOUND": "検知", "ERROR": "エラー", "OK": "正常"}
_QUARANTINED_LABEL = "隔離済み"
#: 結果の行のパスの番号（ScanScreen._row_paths の位置）を入れる場所
_PATH_INDEX_ROLE = Qt.ItemDataRole.UserRole
#: 検査できなかったファイルの判定列の表示。サイズの上限で検査していないものは、
#: 検査済みと誤解されないよう、ほかのエラーと区別して表示する
_ERROR_REASON_LABELS = {
    ERROR_REASON_SIZE_LIMIT: "未検査（サイズの上限）",
    ERROR_REASON_PERMISSION: "未検査（権限なし）",
    ERROR_REASON_VANISHED: "未検査（削除済み）",
    ERROR_REASON_OTHER: "エラー",
}

#: スキャン履歴の表示（core/scan_history_service.py の値）
_HISTORY_TYPES = {"quick": "クイック", "full": "フル", "custom": "カスタム", "schedule": "スケジュール"}
_HISTORY_SOURCES = {"gui": "画面", "api": "Web API", "schedule": "スケジュール"}
_HISTORY_RESULTS = {"completed": "完了", "cancelled": "中断", "failed": "失敗"}
_HISTORY_ACTIONS = {"quarantined": "隔離しました", "notified": "通知のみ"}
_HISTORY_REASONS = {"size_limit": "サイズの上限", "permission": "権限なし", "vanished": "削除済み", "other": "エラー"}


class ScanScreen(BaseScreen):
    screen_title = "ウイルス スキャン"
    #: 結果の行の、検査したファイルのパスそのもの（表示の文字は見える形に変えているため、隔離・無視にはこちらを使う）
    _row_paths: list[str]
    screen_subtitle = "ClamAV のスキャン エンジンで、ファイル・フォルダー・ドライブを検査します。"

    def build_content(self) -> None:
        self._row_paths = []
        # 1. スキャン対象選択エリア
        self.begin_card("スキャン対象の選択")
        self.quick_radio = QRadioButton("クイックスキャン（デスクトップ・ダウンロード・ドキュメント・一時フォルダ等）")
        self.full_radio = QRadioButton("フルスキャン（システムドライブ全体）")
        self.custom_radio = QRadioButton("カスタム（ファイル/フォルダ選択）")
        self.quick_radio.setChecked(True)
        for r in (self.quick_radio, self.full_radio, self.custom_radio):
            self.add_layout_widget(r)

        custom_path_layout = QHBoxLayout()
        self.custom_path_list = QListWidget()
        custom_buttons = QVBoxLayout()
        self.browse_button = QPushButton("フォルダ追加")
        self.add_file_button = QPushButton("ファイル追加")
        self.remove_path_button = QPushButton("削除")
        self.browse_button.clicked.connect(self._on_browse_clicked)
        self.add_file_button.clicked.connect(self._on_add_file_clicked)
        self.remove_path_button.clicked.connect(self._on_remove_path_clicked)
        for btn in (self.browse_button, self.add_file_button, self.remove_path_button):
            custom_buttons.addWidget(btn)
        custom_buttons.addStretch(1)
        custom_path_layout.addWidget(self.custom_path_list)
        custom_path_layout.addLayout(custom_buttons)
        self._add_hlayout(custom_path_layout)

        # 2. オプションエリア
        self.begin_card("スキャン オプション")
        self.include_subfolder_check = QCheckBox("サブフォルダを含む")
        self.include_subfolder_check.setChecked(True)
        self.apply_exclude_check = QCheckBox("除外リストを適用")
        self.apply_exclude_check.setChecked(True)
        self.add_layout_widget(self.include_subfolder_check)
        self.add_layout_widget(self.apply_exclude_check)

        # 3. 実行制御エリア
        self.begin_card("スキャンの実行")
        control_layout = QHBoxLayout()
        self.start_button = set_variant(QPushButton("スキャン開始"), "primary")
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self._on_start_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.state_label = plain(QLabel("状態: 待機中"))
        control_layout.addWidget(self.start_button)
        control_layout.addWidget(self.stop_button)
        control_layout.addWidget(self.state_label)
        control_layout.addStretch(1)
        self._add_hlayout(control_layout)

        self.progress_bar = QProgressBar()
        self.current_file_label = plain(QLabel(""))  # ファイル名を文字のまま表示する
        self.counter_label = QLabel(_counter_text(0, 0, 0))
        self.add_layout_widget(self.progress_bar)
        self.add_layout_widget(self.current_file_label)
        self.add_layout_widget(self.counter_label)

        # 4. 結果一覧テーブル（検知・エラーを都度追記。正常ファイルは件数のみ）
        self.begin_card("スキャン結果")
        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(["ファイルパス", "判定", "ウイルス名 / 内容", "検知日時"])
        self.add_layout_widget(self.result_table)

        result_action_layout = QHBoxLayout()
        self.quarantine_button = QPushButton("隔離する")
        self.ignore_button = QPushButton("無視する")
        self.quarantine_button.clicked.connect(self._on_quarantine_clicked)
        self.ignore_button.clicked.connect(self._on_ignore_clicked)
        result_action_layout.addWidget(self.quarantine_button)
        result_action_layout.addWidget(self.ignore_button)
        self._add_hlayout(result_action_layout)

        # 5. スキャン履歴（トレイGUI だけ。サービスが記録した、画面・スケジュール・Web API のスキャン。段階 12-4b）
        self.history_card = self.begin_card("スキャン履歴", "画面・スケジュール・Web API から行ったスキャンの記録です。")
        self.history_table = QTableWidget(0, 7)
        self.history_table.setHorizontalHeaderLabels(
            ["開始日時", "種類", "実行元", "結果", "スキャン", "検知", "検査できなかったファイル"])
        self.history_table.itemDoubleClicked.connect(lambda _item: self._on_history_detail_clicked())
        self.add_layout_widget(self.history_table)
        history_buttons = QHBoxLayout()
        self.history_detail_button = QPushButton("詳細")
        self.history_reload_button = QPushButton("最新の情報に更新")
        self.history_detail_button.clicked.connect(self._on_history_detail_clicked)
        self.history_reload_button.clicked.connect(self.reload_history)
        history_buttons.addWidget(self.history_detail_button)
        history_buttons.addWidget(self.history_reload_button)
        history_buttons.addStretch(1)
        self._add_hlayout(history_buttons)
        self.history_card.setVisible(False)
        self._history_service = None

        self._scanned_count = 0
        self._detected_count = 0
        self._error_count = 0
        self._scan_dispatcher = None
        self._quarantine_service = None
        # 非機能要件詳細2.3節: 進捗・件数の画面更新は最短200ms間隔でまとめて反映する
        # （ファイルごとに再描画しない。検知・エラー行の追加は即時）
        self._latest_progress: tuple[int, int, str] | None = None
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.setInterval(_VIEW_UPDATE_INTERVAL_MS)
        self._view_timer.timeout.connect(self._flush_view)

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(self, scan_dispatcher, quarantine_service) -> None:
        """トレイGUI（tray_main.py）の起動から呼び出し、サービス層を注入する"""
        self._scan_dispatcher = scan_dispatcher
        self._quarantine_service = quarantine_service
        self._scan_dispatcher.progress.connect(self._on_progress)
        self._scan_dispatcher.file_scanned.connect(self._on_file_scanned)
        self._scan_dispatcher.finished.connect(self._on_finished)
        self._scan_dispatcher.error.connect(self._on_scan_error)
        self._scan_dispatcher.activity.connect(self._on_activity)

    def bind_history(self, history_service) -> None:
        """スキャン履歴の一覧を表示する（トレイGUI。api_client.proxies.ScanHistoryProxy）"""
        self._history_service = history_service
        self.history_card.setVisible(True)
        self.reload_history()

    def reload_history(self) -> None:
        if self._history_service is None:
            return
        try:
            records = self._history_service.list_recent()
        except AppError as exc:
            logger.warning(f"スキャン履歴を読めませんでした ({exc.code}: {exc.detail or exc.message})")
            return
        self.history_table.setRowCount(0)
        for record in records:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            values = [
                local_time_text(record.started_at),
                record.label or _HISTORY_TYPES.get(record.scan_type, record.scan_type),
                _HISTORY_SOURCES.get(record.source, record.source),
                _HISTORY_RESULTS.get(record.result, record.result),
                str(record.scanned), str(record.detected), str(record.errors),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(plain_tooltip(value))
                self.history_table.setItem(row, col, item)
            self.history_table.item(row, 0).setData(_PATH_INDEX_ROLE, record.id)

    def _on_history_detail_clicked(self) -> None:
        row = self.history_table.currentRow()
        if self._history_service is None or row < 0:
            return
        try:
            data = self._history_service.get(self.history_table.item(row, 0).data(_PATH_INDEX_ROLE))
        except AppError as exc:
            show_error_dialog(self, exc)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("スキャン履歴の詳細")
        dialog.resize(720, 480)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        text.setPlainText(history_detail_text(data))  # ファイル名は文字のまま表示する
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    # -- UIイベント -----------------------------------------------------
    def _on_browse_clicked(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "スキャン対象フォルダを選択")
        self._add_custom_path(path)

    def _on_add_file_clicked(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "スキャン対象ファイルを選択")
        for path in paths:
            self._add_custom_path(path)

    def _add_custom_path(self, path: str) -> None:
        if not path:
            return
        existing = {self.custom_path_list.item(i).text() for i in range(self.custom_path_list.count())}
        if path not in existing:
            self.custom_path_list.addItem(path)
        self.custom_radio.setChecked(True)

    def _on_remove_path_clicked(self) -> None:
        for item in self.custom_path_list.selectedItems():
            self.custom_path_list.takeItem(self.custom_path_list.row(item))

    def start_quick_scan(self) -> bool:
        """トレイメニュー「今すぐスキャン」から呼ばれる（クイックスキャンを即実行）"""
        self.quick_radio.setChecked(True)
        return self._on_start_clicked()

    def _on_start_clicked(self) -> bool:
        if self._scan_dispatcher is None or self._scan_dispatcher.is_running:
            return False

        targets = self._resolve_targets()
        if not targets:
            QMessageBox.warning(self, "スキャン対象", "スキャン対象のファイルまたはフォルダを追加してください。")
            return False
        # 画面設計書2章: 対象パスの存在確認
        missing = [t for t in targets if not Path(t).exists()]
        if missing:
            QMessageBox.warning(
                self, "スキャン対象", "次のスキャン対象が見つかりません:\n" + "\n".join(missing)
            )
            return False

        self._show_running()
        self._scan_dispatcher.start(
            targets,
            include_subfolders=self.include_subfolder_check.isChecked(),
            apply_exclude=self.apply_exclude_check.isChecked(),
            label=self._scan_label(),
            skip_dirs=scan_targets.full_scan_skip_dirs() if self.full_radio.isChecked() else (),
            scan_type="quick" if self.quick_radio.isChecked() else "full" if self.full_radio.isChecked() else "custom",
        )
        return True

    def show_started_elsewhere(self) -> None:
        """画面以外（Web API）から始めたスキャンを、実行中として表示する（結果はシグナルで届く）"""
        self._show_running()

    def _show_running(self) -> None:
        self.result_table.setRowCount(0)
        self._row_paths = []
        self._scanned_count = 0
        self._detected_count = 0
        self._error_count = 0
        self.progress_bar.setRange(0, 0)  # 対象ファイルの列挙中は不確定表示
        self.counter_label.setText(_counter_text(0, 0, 0))
        self.state_label.setText("状態: スキャン中")

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def _scan_label(self) -> str:
        if self.quick_radio.isChecked():
            return "クイックスキャン"
        if self.full_radio.isChecked():
            return "フルスキャン"
        return "カスタムスキャン"

    def _on_stop_clicked(self) -> None:
        if self._scan_dispatcher is not None:
            self._scan_dispatcher.stop()

    def _resolve_targets(self) -> list[str]:
        if self.quick_radio.isChecked():
            return scan_targets.quick_scan_targets()
        if self.full_radio.isChecked():
            return scan_targets.full_scan_targets()
        return [self.custom_path_list.item(i).text() for i in range(self.custom_path_list.count())]

    # -- ScanDispatcherからのシグナル受信 -----------------------------------
    def _on_progress(self, scanned: int, total: int, current_path: str) -> None:
        self._latest_progress = (scanned, total, current_path)
        self._schedule_view_update()

    def _on_file_scanned(self, path: str, status: str, detail: str, detected_at: str) -> None:
        self._scanned_count += 1
        if status == "FOUND":
            self._detected_count += 1
        elif status == "ERROR":
            self._error_count += 1
        if status in ("FOUND", "ERROR"):
            self._add_result_row(path, status, detail, detected_at)
        self._schedule_view_update()

    def _on_activity(self, state: str, path: str, elapsed_sec: int) -> None:
        """待機中・応答待ちの表示（clamdソケット通信プロトコル設計書4章・5章）"""
        if not self.stop_button.isEnabled():
            return  # スキャン終了後に届いた通知は無視する
        if state == "queued":
            self.state_label.setText("状態: 待機中（リアルタイム監視のスキャンを優先しています）")
        elif state == "stalled":
            self.state_label.setText(
                f"状態: 応答待ち（{elapsed_sec}秒）: {Path(path).name}　※「停止」で中断できます"
            )
            self.state_label.setToolTip(plain_tooltip(path))
        else:
            self.state_label.setText("状態: スキャン中")
            self.state_label.setToolTip("")

    def _schedule_view_update(self) -> None:
        if not self._view_timer.isActive():
            self._view_timer.start()

    def _flush_view(self) -> None:
        self._view_timer.stop()
        if self._latest_progress is not None:
            scanned, total, current_path = self._latest_progress
            # トレイGUI では、問題の無いファイルの結果（file_scanned）は届かない。進み具合の件数を使う
            self._scanned_count = max(self._scanned_count, scanned)
            if total > 0:
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(scanned)
            self.current_file_label.setText(current_path)
        self.counter_label.setText(_counter_text(self._detected_count, self._error_count, self._scanned_count))

    def _on_finished(self, summary: dict) -> None:
        if isinstance(summary.get("scanned"), int):
            self._scanned_count = max(self._scanned_count, summary["scanned"])
        self._flush_view()
        self._latest_progress = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(self.progress_bar.maximum())
        self.current_file_label.setText("")
        state = "状態: 中断" if summary.get("cancelled") else "状態: 完了"
        if self._error_count:
            state += f"（検査できなかったファイルが {self._error_count} 件あります。スキャン結果の一覧を確認してください）"
        self.state_label.setText(state)

    def _on_scan_error(self, error) -> None:
        show_error_dialog(self, error)

    def _add_result_row(self, path: str, status: str, detail: str, detected_at: str) -> None:
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        if status == "ERROR":
            label = _ERROR_REASON_LABELS[error_reason(detail)]
        else:
            label = _STATUS_LABELS.get(status, status)
        # 表示は見える形にする（UTF-8 にできない名前の文字は、Qt の文字列では消えて別のファイルの名前に見えるため）
        values = [visible_controls(path), label, detail, local_time_text(detected_at)]
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(plain_tooltip(value))  # 列の幅で切れた内容（検査できなかった理由等）も読めるようにする
            self.result_table.setItem(row, col, item)
        # 隔離・無視には表示の文字ではなく、検査したファイルのパスそのものを使う（行に番号を付けて引く）
        self._row_paths.append(path)
        self.result_table.item(row, 0).setData(_PATH_INDEX_ROLE, len(self._row_paths) - 1)

    def _row_path(self, row: int) -> str:
        return self._row_paths[self.result_table.item(row, 0).data(_PATH_INDEX_ROLE)]

    def _selected_detection_rows(self) -> list[int]:
        rows = sorted({idx.row() for idx in self.result_table.selectedIndexes()})
        return [r for r in rows if self.result_table.item(r, 1).text() == _STATUS_LABELS["FOUND"]]

    # -- 検知行へのアクション -------------------------------------------------
    def _on_quarantine_clicked(self) -> None:
        if self._quarantine_service is None:
            return
        selected_rows = self._selected_detection_rows()
        if not selected_rows:
            return

        if not show_confirm_dialog(self, "隔離の確認", "選択したファイルを隔離しますか？"):
            return

        for row in selected_rows:
            path = self._row_path(row)
            virus_name = self.result_table.item(row, 2).text()
            try:
                self._quarantine_service.quarantine(path, virus_name)
                self.result_table.item(row, 1).setText(_QUARANTINED_LABEL)
            except Exception as exc:  # AppErrorはそのまま、それ以外もダイアログで通知
                from core.exceptions import AppError, QuarantineWriteError

                err = exc if isinstance(exc, AppError) else QuarantineWriteError(detail=str(exc))
                # どのファイルの隔離ができなかったかを、後から追えるように残す
                log_event(CATEGORY_SCAN, f"検知したファイルを隔離できませんでした: {path} ({virus_name})",
                          level=logging.WARNING, error_code=err.code)
                show_error_dialog(self, err)

    def _on_ignore_clicked(self) -> None:
        """検知行を一覧から外す（ファイルには何もしない。対応しなかったことをログに残す）"""
        for row in reversed(self._selected_detection_rows()):
            path = self._row_path(row)
            virus_name = self.result_table.item(row, 2).text()
            log_event(
                CATEGORY_SCAN,
                f"検知を無視しました（ファイルは残っています）: {path} ({virus_name})",
                level=logging.WARNING,
            )
            self.result_table.removeRow(row)

    def _add_hlayout(self, layout: QHBoxLayout) -> None:
        self.add_layout_widget(wrap_layout(layout))


def history_detail_text(data: dict) -> str:
    """スキャン履歴の詳細（GET /scans/history/{id}）を、読める文章にする"""
    kind = data.get("label") or _HISTORY_TYPES.get(data.get("scan_type", ""), data.get("scan_type", ""))
    result = _HISTORY_RESULTS.get(data.get("result", ""), data.get("result", ""))
    if data.get("detail"):
        result += f"（{data['detail']}）"
    source = _HISTORY_SOURCES.get(data.get("source", ""), data.get("source", ""))
    lines = [
        f"種類: {kind}（{source}）",
        f"開始: {local_time_text(data.get('started_at', ''))}　終了: {local_time_text(data.get('finished_at', ''))}",
        f"結果: {result}",
        f"件数: {_counter_text(data.get('detected', 0), data.get('errors', 0), data.get('scanned', 0))}",
        "",
        "対象:",
        *[f"  {target}" for target in data.get("targets", [])],
    ]
    detections = data.get("detections", [])
    if detections:
        # 扱いはスキャンの時点のもの（後でスキャン画面から隔離したものは「通知のみ」のまま）
        lines += ["", f"検知したファイル（{len(detections)} 件。扱いはスキャンの時点のもの）:"]
        for d in detections:
            action = _HISTORY_ACTIONS.get(d.get("action"), d.get("action") or "")
            lines.append(f"  {d.get('path', '')}: {d.get('virus_name', '')}（{action}）")
    error_files = data.get("error_files", [])
    if error_files:
        lines += ["", f"検査できなかったファイル（{data.get('errors', len(error_files))} 件）:"]
        for e in error_files:
            reason = _HISTORY_REASONS.get(e.get("reason"), e.get("reason") or "")
            detail = f"（{e['detail']}）" if e.get("detail") else ""
            lines.append(f"  {e.get('path', '')}: {reason}{detail}")
        if data.get("errors_truncated"):
            lines.append(f"  ほかに {data['errors_truncated']} 件（記録は {MAX_ERRORS:,} 件まで）")
    return "\n".join(lines)


def _counter_text(detected: int, errors: int, scanned: int) -> str:
    return f"検知件数: {detected} / 検査できなかったファイル: {errors} / スキャン件数: {scanned}"
