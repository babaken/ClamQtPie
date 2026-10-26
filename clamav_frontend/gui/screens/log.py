"""
ログ画面

参照: 画面設計書7章
Phase8: LogReaderServiceとの結線を追加
"""
from __future__ import annotations

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.exceptions import AppError
from core.log_reader_service import LOG_CATEGORIES
from gui.error_dialog import show_confirm_dialog, show_error_dialog
from gui.screens.base import BaseScreen


class LogScreen(BaseScreen):
    screen_title = "保護の履歴"
    screen_subtitle = "スキャン・検知・隔離・定義更新などの記録（アプリのログ）を確認できます。"

    def build_content(self) -> None:
        filter_group = QGroupBox("絞り込み")
        filter_layout = QVBoxLayout(filter_group)

        date_layout = QHBoxLayout()
        self.date_from_edit = QDateEdit()
        self.date_from_edit.setDate(QDate.currentDate().addDays(-7))
        self.date_to_edit = QDateEdit()
        self.date_to_edit.setDate(QDate.currentDate())
        date_layout.addWidget(self.date_from_edit)
        date_layout.addWidget(self.date_to_edit)
        filter_layout.addLayout(date_layout)

        level_layout = QHBoxLayout()
        self.level_checks: dict[str, QCheckBox] = {}
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            cb = QCheckBox(level)
            cb.setChecked(True)
            cb.stateChanged.connect(self._on_filter_changed)
            self.level_checks[level] = cb
            level_layout.addWidget(cb)
        filter_layout.addLayout(level_layout)

        # 画面設計書7章: カテゴリ（チェックボックス群、既定は全選択）
        category_layout = QHBoxLayout()
        self.category_checks: dict[str, QCheckBox] = {}
        for category in LOG_CATEGORIES:
            cb = QCheckBox(category)
            cb.setChecked(True)
            cb.stateChanged.connect(self._on_filter_changed)
            self.category_checks[category] = cb
            category_layout.addWidget(cb)
        filter_layout.addLayout(category_layout)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("キーワード検索（エラーコード等）")
        self.search_edit.textChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self.search_edit)

        self.date_from_edit.dateChanged.connect(self._on_filter_changed)
        self.date_to_edit.dateChanged.connect(self._on_filter_changed)

        self.add_layout_widget(filter_group)

        self.log_table = QTableWidget(0, 4)
        self.log_table.setHorizontalHeaderLabels(["日時", "レベル", "カテゴリ", "メッセージ"])
        self.add_layout_widget(self.log_table)

        button_frame = QFrame()
        button_layout = QHBoxLayout(button_frame)
        button_layout.setContentsMargins(0, 0, 0, 0)
        self.export_csv_button = QPushButton("CSV出力")
        self.export_csv_button.clicked.connect(self._on_export_csv_clicked)
        button_layout.addWidget(self.export_csv_button)
        self.clear_button = QPushButton("ログをクリア")
        self.clear_button.setToolTip("すべてのログ（過去の日付のファイルを含む）を削除します")
        self.clear_button.clicked.connect(self._on_clear_clicked)
        button_layout.addWidget(self.clear_button)
        button_layout.addStretch(1)
        self.add_layout_widget(button_frame)

        self._log_reader_service = None
        self._all_entries: list = []
        self._current_filtered: list = []
        #: 期間の既定値を決めた日（常駐したまま日をまたいだときに期間を今日へ進めるため。on_shown）
        self._period_base_date = QDate.currentDate()

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(self, log_reader_service) -> None:
        self._log_reader_service = log_reader_service
        self.reload()

    def reload(self) -> None:
        if self._log_reader_service is None:
            return
        self._all_entries = self._log_reader_service.read_entries()
        self._apply_filter()

    def on_shown(self) -> None:
        """画面を開いたときに呼ばれる（app.pyの画面切り替え）。最新のログを読み直す

        以前は起動時と「詳細を見る」のときしか読み直さず、期間の終了日も起動した日のままだったため、
        常駐していると最近の検知が表示されなかった（コードレビューで修正）。
        終了日が前回の「今日」のままなら、同じ日数の期間のまま今日まで進める。
        利用者が終了日を変えていた場合はそのままにする。
        """
        today = QDate.currentDate()
        base = self._period_base_date
        if today != base and self.date_to_edit.date() == base:
            span = self.date_from_edit.date().daysTo(base)
            for edit, value in ((self.date_to_edit, today), (self.date_from_edit, today.addDays(-span))):
                edit.blockSignals(True)
                edit.setDate(value)
                edit.blockSignals(False)
        self._period_base_date = today
        self.reload()

    def _on_filter_changed(self, *_args) -> None:
        self._apply_filter()

    def _apply_filter(self) -> None:
        if self._log_reader_service is None:
            return
        from datetime import datetime

        levels = {name for name, cb in self.level_checks.items() if cb.isChecked()}
        date_from = datetime(
            self.date_from_edit.date().year(),
            self.date_from_edit.date().month(),
            self.date_from_edit.date().day(),
        )
        date_to_qdate = self.date_to_edit.date().addDays(1)
        date_to = datetime(date_to_qdate.year(), date_to_qdate.month(), date_to_qdate.day())
        keyword = self.search_edit.text().strip()

        categories = {name for name, cb in self.category_checks.items() if cb.isChecked()}
        filtered = self._log_reader_service.filter_entries(
            self._all_entries,
            levels=levels,
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
            categories=categories,
        )
        self._render_table(filtered)
        self._current_filtered = filtered

    def _render_table(self, entries: list) -> None:
        self.log_table.setRowCount(0)
        for entry in entries:
            row = self.log_table.rowCount()
            self.log_table.insertRow(row)
            values = [
                entry.datetime.strftime("%Y-%m-%d %H:%M:%S"),
                entry.level,
                entry.category,
                entry.message,
            ]
            for col, value in enumerate(values):
                self.log_table.setItem(row, col, QTableWidgetItem(value))

    def _on_export_csv_clicked(self) -> None:
        if self._log_reader_service is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "CSV出力先を選択", "log_export.csv", "CSV (*.csv)")
        if not path:
            return
        from pathlib import Path

        self._log_reader_service.export_csv(self._current_filtered, Path(path))
        QMessageBox.information(self, "CSV出力", "CSVファイルを出力しました。")

    def _on_clear_clicked(self) -> None:
        if self._log_reader_service is None:
            return
        # 画面設計書「共通仕様」: 破壊的操作は確認ダイアログを挟む
        if not show_confirm_dialog(
            self,
            "ログのクリア",
            "すべてのログ（過去の日付のファイルを含む）を削除します。元に戻せません。\n"
            "必要なら先に「CSV出力」で保存してください。\n\nログをクリアしますか？",
        ):
            return
        try:
            self._log_reader_service.clear()
        except OSError as exc:
            show_error_dialog(self, AppError("ログをクリアできませんでした", detail=str(exc)))
        self.reload()

    def filter_by_error_code(self, code: str) -> None:
        """エラーダイアログの「詳細を見る」から遷移してきた際、検索欄へコードを自動入力する"""
        self.filter_by_keyword(code)

    def filter_by_keyword(self, keyword: str) -> None:
        """他画面からの遷移時に検索欄へキーワード（エラーコード・ジョブ名等）を入力し、最新のログを読み直す"""
        self.reload()
        self.search_edit.setText(keyword)
