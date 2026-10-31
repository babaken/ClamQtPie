"""
定義更新画面

参照: 画面設計書6章
Phase6: UpdateDispatcherとの結線を追加
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
)

from gui.error_dialog import show_error_dialog
from gui.screens.base import BaseScreen
from gui.theme import set_variant
from gui.plain_text import plain


class UpdateScreen(BaseScreen):
    screen_title = "ウイルス定義の更新"
    screen_subtitle = "freshclam を使用して、最新のウイルス定義データベースをダウンロードします。"

    def build_content(self) -> None:
        self.begin_card("ウイルス データベースと定義の状態")
        self.version_label = plain(QLabel("現在の定義DBバージョン: - / 最終更新: -"))
        self.add_layout_widget(self.version_label)

        # 画面設計書6章3: 自動更新設定（本画面では表示のみ、設定画面へのショートカット）
        auto_update_layout = QHBoxLayout()
        self.auto_update_label = QLabel("自動更新: -")
        self.open_settings_button = QPushButton("設定を変更")
        self.open_settings_button.clicked.connect(self._on_open_settings_clicked)
        auto_update_layout.addWidget(self.auto_update_label)
        auto_update_layout.addWidget(self.open_settings_button)
        auto_update_layout.addStretch(1)
        frame = QFrame()
        frame.setLayout(auto_update_layout)
        self.add_layout_widget(frame)

        self.update_now_button = set_variant(QPushButton("今すぐ更新"), "primary")
        self.update_now_button.clicked.connect(self._on_update_now_clicked)
        self.add_layout_widget(self.update_now_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # 不確定進捗（freshclamの完了予測ができないため）
        self.progress_bar.setVisible(False)
        self.add_layout_widget(self.progress_bar)

        self.begin_card("freshclam の更新出力")
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setPlaceholderText("freshclamの実行ログがここに表示されます")
        self.add_layout_widget(self.log_area)

        self.begin_card("更新履歴")
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(
            ["実行日時", "結果", "更新前バージョン", "更新後バージョン"]
        )
        self.add_layout_widget(self.history_table)

        self._update_service = None
        self._update_dispatcher = None
        self._clamav_service = None
        self._config_service = None
        self._on_open_settings = None

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(
        self, update_service, update_dispatcher, clamav_service, config_service=None, on_open_settings=None
    ) -> None:
        self._update_service = update_service
        self._update_dispatcher = update_dispatcher
        self._clamav_service = clamav_service
        self._config_service = config_service
        self._on_open_settings = on_open_settings

        self._update_dispatcher.log_line.connect(self._on_log_line)
        self._update_dispatcher.finished.connect(self._on_finished)

        self.reload_history()
        self.reload_version_label()
        self.reload_auto_update_label()

    def reload_auto_update_label(self) -> None:
        if self._config_service is None:
            return
        update = self._config_service.current.update
        text = f"有効（{update.interval_hours}時間ごと）" if update.auto_update else "無効"
        self.auto_update_label.setText(f"自動更新: {text}")

    def _on_open_settings_clicked(self) -> None:
        if self._on_open_settings is not None:
            self._on_open_settings()

    def reload_version_label(self) -> None:
        if self._clamav_service is None:
            return
        try:
            _, db_version, db_date = self._clamav_service.get_version()
            self.version_label.setText(f"現在の定義DBバージョン: {db_version} / 最終更新: {db_date}")
        except Exception:  # noqa: BLE001 - clamd未起動時は表示のみ据え置く
            self.version_label.setText("現在の定義DBバージョン: 取得できません（ClamAVサービス未起動）")

    def reload_history(self) -> None:
        if self._update_service is None:
            return
        records = self._update_service.list_history()
        self.history_table.setRowCount(0)
        for record in records:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            result_label = {"success": "成功", "failure": "失敗", "no_change": "変更なし"}.get(
                record.result, record.result
            )
            values = [record.executed_at, result_label, record.before_version, record.after_version]
            for col, value in enumerate(values):
                self.history_table.setItem(row, col, QTableWidgetItem(value))

    # -- 更新実行 -----------------------------------------------------
    def start_update(self) -> None:
        """ホーム画面・トレイの「定義更新」からも呼ばれる（実行中表示を本画面と揃えるため）"""
        self._on_update_now_clicked()

    def _on_update_now_clicked(self) -> None:
        if self._update_dispatcher is None or self._update_dispatcher.is_running:
            return
        self.log_area.clear()
        self.update_now_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        self._update_dispatcher.start()

    def _on_log_line(self, line: str) -> None:
        self.log_area.append(line)

    def _on_finished(self, record) -> None:
        self.update_now_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.reload_history()
        self.reload_version_label()
        if record.result == "failure":
            from core.exceptions import DefinitionUpdateError

            # freshclamのエラー行・Linuxでの対処方法を含めて表示する（UpdateHistoryRecord.detail）
            show_error_dialog(self, DefinitionUpdateError(detail=record.detail or "実行ログを確認してください"))
