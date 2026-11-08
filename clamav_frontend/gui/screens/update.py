"""
定義更新画面

参照: 画面設計書6章
Phase6: UpdateDispatcherとの結線を追加
OS の定義更新サービス（Linux の clamav-freshclam）の状態の表示と、開始・停止（freshclam_service。無い OS では出さない）
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

from core.background_task import BackgroundTask
from core.exceptions import AppError
from gui.error_dialog import show_error_dialog
from gui.screens.base import BaseScreen
from gui.time_text import local_time_text
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

        # OS の定義更新サービス（Linux の clamav-freshclam）。ある OS でだけ出す
        self.freshclam_card = self.begin_card("OS の定義更新サービス (clamav-freshclam)")
        self.freshclam_state_label = QLabel("状態: -")
        self.freshclam_boot_label = QLabel("PC の起動時に自動で起動する: -")
        self.freshclam_summary_label = QLabel("")
        self.freshclam_summary_label.setWordWrap(True)
        for label in (self.freshclam_state_label, self.freshclam_boot_label, self.freshclam_summary_label):
            label.setObjectName("cardText")
            self.add_layout_widget(label)
        freshclam_buttons = QHBoxLayout()
        self.freshclam_start_button = QPushButton("▶ 開始")
        self.freshclam_stop_button = QPushButton("■ 停止")
        self.freshclam_start_button.clicked.connect(lambda: self._set_freshclam_running(True))
        self.freshclam_stop_button.clicked.connect(lambda: self._set_freshclam_running(False))
        freshclam_buttons.addWidget(self.freshclam_start_button)
        freshclam_buttons.addWidget(self.freshclam_stop_button)
        freshclam_buttons.addStretch(1)
        buttons_frame = QFrame()
        buttons_frame.setLayout(freshclam_buttons)
        self.add_layout_widget(buttons_frame)
        hint = QLabel("停止すると、PC の起動時にも起動しなくなり、定義の更新は ClamQtPie の自動更新が行います。"
                      "開始すると、PC の起動時にも自動で起動し、定義の更新は OS のサービスが行います。")
        hint.setObjectName("cardText")
        hint.setWordWrap(True)
        self.add_layout_widget(hint)
        self.freshclam_card.setVisible(False)

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
        self._freshclam_service = None
        #: OS の定義更新サービスの最後に読んだ状態（core.freshclam_service.FreshclamServiceState.to_dict()＋summary）
        self._freshclam_state: dict | None = None
        self._freshclam_task = BackgroundTask()
        self._freshclam_task.finished.connect(self._on_freshclam_task_finished)

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(
        self, update_service, update_dispatcher, clamav_service, config_service=None, on_open_settings=None,
        freshclam_service=None,
    ) -> None:
        """freshclam_service: OS の定義更新サービス（status()・set_running(bool) が状態の dict を返す）。None なら出さない"""
        self._update_service = update_service
        self._update_dispatcher = update_dispatcher
        self._clamav_service = clamav_service
        self._config_service = config_service
        self._on_open_settings = on_open_settings
        self._freshclam_service = freshclam_service

        self._update_dispatcher.log_line.connect(self._on_log_line)
        self._update_dispatcher.finished.connect(self._on_finished)

        self.reload_history()
        self.reload_version_label()
        self.reload_auto_update_label()

    # -- OS の定義更新サービス -----------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 - Qtのオーバーライド
        super().showEvent(event)
        self.reload_freshclam_service()  # 端末などで変えられていることがあるため、開くたびに読み直す

    def reload_freshclam_service(self) -> None:
        """状態を読み直す（systemctl を呼ぶため、別のスレッドで）"""
        if self._freshclam_service is not None:
            self._run_freshclam_task("status", self._freshclam_service.status)

    def _set_freshclam_running(self, running: bool) -> None:
        if self._freshclam_service is not None:
            self._run_freshclam_task("set", lambda: self._freshclam_service.set_running(running))

    def _run_freshclam_task(self, name: str, func) -> None:
        if self._freshclam_task.run(name, func):
            self._update_freshclam_buttons()

    def _on_freshclam_task_finished(self, name: str, result, error) -> None:
        if error is None and isinstance(result, dict):
            self.apply_freshclam_state(result)
        elif error is not None and name == "set":
            show_error_dialog(self, error if isinstance(error, AppError) else AppError(detail=str(error)))
        self._update_freshclam_buttons()

    def apply_freshclam_state(self, state: dict) -> None:
        self._freshclam_state = state
        available = bool(state.get("available"))
        self.freshclam_card.setVisible(available)
        if available:
            self.freshclam_state_label.setText("状態: " + ("動いています" if state.get("active") else "止まっています"))
            self.freshclam_boot_label.setText("PC の起動時に自動で起動する: " + ("する" if state.get("enabled") else "しない"))
            self.freshclam_summary_label.setText(state.get("summary", ""))
        self._update_freshclam_buttons()

    def _update_freshclam_buttons(self) -> None:
        state = self._freshclam_state or {}
        busy = self._freshclam_task.is_running
        # 開始・停止は「今の状態」と「PC の起動時」の両方を揃える。片方だけ違うときは、どちらのボタンも押せる
        self.freshclam_start_button.setEnabled(not busy and not (state.get("active") and state.get("enabled")))
        self.freshclam_stop_button.setEnabled(not busy and bool(state.get("active") or state.get("enabled")))

    def reload_auto_update_label(self) -> None:
        if self._config_service is None:
            return
        update = self._config_service.current.update
        text = f"有効（{update.interval_hours}時間ごと）" if update.auto_update else "無効"
        self.auto_update_label.setText(f"自動更新: {text}")
        self.reload_freshclam_service()  # 誰が定義を更新するかの説明は、自動更新の ON/OFF で変わる

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
            result_label = {"success": "成功", "failure": "失敗", "no_change": "最新"}.get(
                record.result, record.result
            )
            values = [local_time_text(record.executed_at), result_label, record.before_version, record.after_version]
            for col, value in enumerate(values):
                self.history_table.setItem(row, col, QTableWidgetItem(value))

    # -- 更新実行 -----------------------------------------------------
    def start_update(self) -> None:
        """ホーム画面・トレイの「定義更新」からも呼ばれる（実行中表示を本画面と揃えるため）"""
        self._on_update_now_clicked()

    def _on_update_now_clicked(self) -> None:
        if self._update_dispatcher is None or self._update_dispatcher.is_running:
            return
        self.show_started_elsewhere()
        self._update_dispatcher.start()

    def show_started_elsewhere(self) -> None:
        """実行中の表示にする（画面以外（Web API）から始めた更新でも使う。ログはシグナルで届く）"""
        self.log_area.clear()
        self.update_now_button.setEnabled(False)
        self.progress_bar.setVisible(True)

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
