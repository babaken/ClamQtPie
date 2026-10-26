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

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
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
    QVBoxLayout,
)

from core import scan_targets
from core.logger import CATEGORY_SCAN, log_event
from gui.error_dialog import show_confirm_dialog, show_error_dialog
from gui.screens.base import BaseScreen, wrap_layout
from gui.theme import set_variant

_VIEW_UPDATE_INTERVAL_MS = 200

#: 判定列の表示（画面設計書2章: 正常・検知・エラー）
_STATUS_LABELS = {"FOUND": "検知", "ERROR": "エラー", "OK": "正常"}
_QUARANTINED_LABEL = "隔離済み"


class ScanScreen(BaseScreen):
    screen_title = "ウイルス スキャン"
    screen_subtitle = "ClamAV のスキャン エンジンで、ファイル・フォルダー・ドライブを検査します。"

    def build_content(self) -> None:
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
        self.state_label = QLabel("状態: 待機中")
        control_layout.addWidget(self.start_button)
        control_layout.addWidget(self.stop_button)
        control_layout.addWidget(self.state_label)
        control_layout.addStretch(1)
        self._add_hlayout(control_layout)

        self.progress_bar = QProgressBar()
        self.current_file_label = QLabel("")
        self.counter_label = QLabel("検知件数: 0 / スキャン件数: 0")
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

        self._scanned_count = 0
        self._detected_count = 0
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
        """MainWindow/app.py起動シーケンスから呼び出し、サービス層を注入する"""
        self._scan_dispatcher = scan_dispatcher
        self._quarantine_service = quarantine_service
        self._scan_dispatcher.progress.connect(self._on_progress)
        self._scan_dispatcher.file_scanned.connect(self._on_file_scanned)
        self._scan_dispatcher.finished.connect(self._on_finished)
        self._scan_dispatcher.error.connect(self._on_scan_error)
        self._scan_dispatcher.activity.connect(self._on_activity)

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

        self.result_table.setRowCount(0)
        self._scanned_count = 0
        self._detected_count = 0
        self.progress_bar.setRange(0, 0)  # 対象ファイルの列挙中は不確定表示
        self.counter_label.setText("検知件数: 0 / スキャン件数: 0")
        self.state_label.setText("状態: スキャン中")

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        self._scan_dispatcher.start(
            targets,
            include_subfolders=self.include_subfolder_check.isChecked(),
            apply_exclude=self.apply_exclude_check.isChecked(),
            label=self._scan_label(),
            skip_dirs=scan_targets.full_scan_skip_dirs() if self.full_radio.isChecked() else (),
        )
        return True

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
            self.state_label.setToolTip(path)
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
            if total > 0:
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(scanned)
            self.current_file_label.setText(current_path)
        self.counter_label.setText(
            f"検知件数: {self._detected_count} / スキャン件数: {self._scanned_count}"
        )

    def _on_finished(self, summary: dict) -> None:
        self._flush_view()
        self._latest_progress = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(self.progress_bar.maximum())
        self.current_file_label.setText("")
        self.state_label.setText("状態: 中断" if summary.get("cancelled") else "状態: 完了")

    def _on_scan_error(self, error) -> None:
        show_error_dialog(self, error)

    def _add_result_row(self, path: str, status: str, detail: str, detected_at: str) -> None:
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        values = [path, _STATUS_LABELS.get(status, status), detail, detected_at]
        for col, value in enumerate(values):
            self.result_table.setItem(row, col, QTableWidgetItem(value))

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
            path = self.result_table.item(row, 0).text()
            virus_name = self.result_table.item(row, 2).text()
            try:
                self._quarantine_service.quarantine(path, virus_name)
                self.result_table.item(row, 1).setText(_QUARANTINED_LABEL)
            except Exception as exc:  # AppErrorはそのまま、それ以外もダイアログで通知
                from core.exceptions import AppError, QuarantineWriteError

                err = exc if isinstance(exc, AppError) else QuarantineWriteError(detail=str(exc))
                show_error_dialog(self, err)

    def _on_ignore_clicked(self) -> None:
        """検知行を一覧から外す（ファイルには何もしない。対応しなかったことをログに残す）"""
        for row in reversed(self._selected_detection_rows()):
            path = self.result_table.item(row, 0).text()
            virus_name = self.result_table.item(row, 2).text()
            log_event(
                CATEGORY_SCAN,
                f"検知を無視しました（ファイルは残っています）: {path} ({virus_name})",
                level=logging.WARNING,
            )
            self.result_table.removeRow(row)

    def _add_hlayout(self, layout: QHBoxLayout) -> None:
        self.add_layout_widget(wrap_layout(layout))
