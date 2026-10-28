"""
設定画面

参照: 画面設計書8章（8-1〜8-7のサブタブ構成）
Phase7: SMTPテスト送信ボタンをNotificationServiceと結線、
        保存/読込をConfigServiceと結線
コードレビュー後: 「ClamAV エンジン」タブ（8-7。clamdの接続・サービス、clamonaccの場所）を追加。
        接続の設定はアプリの再起動後に反映し、「接続テスト」で保存前の入力値を試せる
"""
from __future__ import annotations

import dataclasses
import sys

from PySide6.QtCore import QDir
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QWidget,
)

from core.background_task import BackgroundTask
from core.config_model import ClamdConfig, RealtimeConfig
from core.exceptions import AppError
from gui.error_dialog import show_error_dialog
from gui.screens.base import BaseScreen, wrap_layout
from gui.theme import COLOR_NG, COLOR_OK, set_variant

_IS_WINDOWS = sys.platform == "win32"
#: 空欄で保存したときに戻す既定値
_DEFAULT_CLAMD = ClamdConfig()
_DEFAULT_CLAMONACC_PATH = RealtimeConfig().clamonacc_path


class SettingsScreen(BaseScreen):
    screen_title = "設定"
    screen_subtitle = "ClamAV エンジンとの連携、通知、リアルタイム監視、定義更新、ログの設定を管理します。"

    def build_content(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_general_tab(), "一般")
        self.tabs.addTab(self._build_engine_tab(), "ClamAV エンジン")
        self.tabs.addTab(self._build_smtp_tab(), "SMTP")
        self.tabs.addTab(self._build_notification_tab(), "通知")
        self.tabs.addTab(self._build_realtime_tab(), "リアルタイム監視")
        self.tabs.addTab(self._build_update_tab(), "定義更新")
        self.tabs.addTab(self._build_logging_tab(), "ログ")
        self.add_layout_widget(self.tabs)

        save_layout = QHBoxLayout()
        self.save_button = set_variant(QPushButton("保存"), "primary")
        self.cancel_button = QPushButton("キャンセル")
        self.save_button.clicked.connect(self._on_save_clicked)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        save_layout.addStretch(1)
        save_layout.addWidget(self.cancel_button)
        save_layout.addWidget(self.save_button)
        from PySide6.QtWidgets import QFrame

        frame = QFrame()
        frame.setLayout(save_layout)
        self.add_layout_widget(frame)

        self._config_service = None
        self._notification_service = None
        self._autostart_service = None
        self._on_saved = None
        self._connection_tester = None
        self._connection_task = BackgroundTask()
        self._connection_task.finished.connect(self._on_connection_test_finished)
        self._update_engine_fields()

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(
        self,
        config_service,
        notification_service,
        autostart_service=None,
        on_saved=None,
        connection_tester=None,
    ) -> None:
        """on_saved(config): 保存直後に呼ばれる。ログ設定・自動更新ジョブ等の即時反映をapp.py側で行う
        （画面設計書8章「即時反映が必要な項目は保存直後に該当サービスへ再読込を通知する」）
        connection_tester(ClamdConfig) -> str: 「接続テスト」の実処理（core.clamd_client.check_connection）。
            成功時は表示する文字列を返し、失敗時は例外を送出する。バックグラウンドで呼ぶ"""
        self._config_service = config_service
        self._notification_service = notification_service
        self._autostart_service = autostart_service
        self._on_saved = on_saved
        self._connection_tester = connection_tester
        self.smtp_test_button.clicked.connect(self._on_test_send_clicked)
        self.reload_from_config()

    def reload_from_config(self) -> None:
        if self._config_service is None:
            return
        config = self._config_service.current

        self.autostart_check.setChecked(config.general.autostart)
        self.tray_hint_check.setChecked(not config.general.show_tray_hint_shown)

        self.connection_type_combo.setCurrentIndex(
            max(self.connection_type_combo.findData(config.clamd.connection_type), 0)
        )
        self.tcp_host_edit.setText(config.clamd.tcp_host)
        self.tcp_port_spin.setValue(config.clamd.tcp_port)
        self.unix_socket_edit.setText(config.clamd.unix_socket_path)
        self.service_name_edit.setText(config.clamd.service_name)
        self.clamd_auto_start_check.setChecked(config.clamd.auto_start)
        self.clamonacc_path_edit.setText(config.realtime.clamonacc_path)
        self.connection_test_label.setText("")
        self._update_engine_fields()

        self.smtp_host_edit.setText(config.smtp.host)
        self.smtp_port_spin.setValue(config.smtp.port)
        self.smtp_user_edit.setText(config.smtp.user)
        self.smtp_from_edit.setText(config.smtp.from_address)
        self.smtp_to_edit.setText(config.smtp.to_address)
        self.smtp_encryption_combo.setCurrentText(config.smtp.encryption)

        self.on_detect_mail_check.setChecked(config.notification.on_detect_mail)
        self.on_detect_desktop_check.setChecked(config.notification.on_detect_desktop)
        self.on_update_fail_check.setChecked(config.notification.on_update_fail)
        self.on_job_fail_check.setChecked(config.notification.on_job_fail)

        self.realtime_default_enabled_check.setChecked(config.realtime.default_enabled)
        self.realtime_default_action_combo.setCurrentIndex(
            max(self.realtime_default_action_combo.findData(config.realtime.default_action), 0)
        )

        self.auto_update_check.setChecked(config.update.auto_update)
        self.update_interval_spin.setValue(config.update.interval_hours)
        self.freshclam_path_edit.setText(config.update.freshclam_path)

        self.retention_days_combo.setCurrentText(str(config.logging.retention_days))
        self.log_level_combo.setCurrentText(config.logging.level)

    def _on_save_clicked(self) -> None:
        if self._config_service is None:
            return
        config = self._config_service.current

        previous_autostart = config.general.autostart
        config.general.autostart = self.autostart_check.isChecked()
        # タスクトレイ常駐アプリプロセス設計書4.1節: トレイ格納時の案内吹き出しをOFFにできる
        # （チェックONで「未表示」に戻し、次回の×ボタン時に再び案内する）
        config.general.show_tray_hint_shown = not self.tray_hint_check.isChecked()

        # ClamAV エンジン: 接続の設定はアプリの再起動後に反映（app.pyのClamAVServiceは起動時の値の写しを使う）。
        # サービス名・自動起動は保存直後から使う。空欄は既定値に戻す
        entered = self._entered_clamd_config(config.clamd)
        config.clamd.connection_type = entered.connection_type
        config.clamd.tcp_host = entered.tcp_host
        config.clamd.tcp_port = entered.tcp_port
        config.clamd.unix_socket_path = entered.unix_socket_path
        config.clamd.service_name = self.service_name_edit.text().strip()
        config.clamd.auto_start = self.clamd_auto_start_check.isChecked()
        config.realtime.clamonacc_path = self.clamonacc_path_edit.text().strip() or _DEFAULT_CLAMONACC_PATH
        self.tcp_host_edit.setText(config.clamd.tcp_host)
        self.unix_socket_edit.setText(config.clamd.unix_socket_path)
        self.service_name_edit.setText(config.clamd.service_name)
        self.clamonacc_path_edit.setText(config.realtime.clamonacc_path)

        config.smtp.host = self.smtp_host_edit.text().strip()
        config.smtp.port = self.smtp_port_spin.value()
        config.smtp.user = self.smtp_user_edit.text().strip()
        config.smtp.from_address = self.smtp_from_edit.text().strip()
        config.smtp.to_address = self.smtp_to_edit.text().strip()
        config.smtp.encryption = self.smtp_encryption_combo.currentText()

        config.notification.on_detect_mail = self.on_detect_mail_check.isChecked()
        config.notification.on_detect_desktop = self.on_detect_desktop_check.isChecked()
        config.notification.on_update_fail = self.on_update_fail_check.isChecked()
        config.notification.on_job_fail = self.on_job_fail_check.isChecked()

        config.realtime.default_enabled = self.realtime_default_enabled_check.isChecked()
        config.realtime.default_action = self.realtime_default_action_combo.currentData()

        config.update.auto_update = self.auto_update_check.isChecked()
        config.update.interval_hours = self.update_interval_spin.value()
        # 空欄はPATHから探す既定値に戻す（config.jsonスキーマ定義書5章）
        config.update.freshclam_path = self.freshclam_path_edit.text().strip() or "freshclam"
        self.freshclam_path_edit.setText(config.update.freshclam_path)

        config.logging.retention_days = int(self.retention_days_combo.currentText())
        config.logging.level = self.log_level_combo.currentText()

        try:
            self._config_service.save(config)
            password = self.smtp_password_edit.text()
            if password:
                self._config_service.set_smtp_password(password)
                self.smtp_password_edit.clear()
        except AppError as exc:
            show_error_dialog(self, exc)
            return

        try:
            self._apply_autostart(config.general.autostart)
        except AppError as exc:
            # エラーコード一覧E8002: 「設定画面のチェックは元に戻す」（OS側と設定値の食い違いを残さない）
            config.general.autostart = previous_autostart
            self.autostart_check.setChecked(previous_autostart)
            try:
                self._config_service.save(config)
            except AppError:
                pass
            show_error_dialog(self, exc)

        if self._on_saved is not None:
            self._on_saved(config)

    def _apply_autostart(self, enabled: bool) -> None:
        """自動起動設定をOS側（Windows: Runキー / Linux: XDG Autostart）へ即時反映する

        従来は次回アプリ起動時（起動シーケンスのステップ9）にしか反映されず、
        OFFにして保存しても次のOS起動時にはまだ自動起動してしまっていた。
        """
        if self._autostart_service is None:
            return
        if enabled and not self._autostart_service.is_enabled():
            self._autostart_service.enable()
        elif not enabled and self._autostart_service.is_enabled():
            self._autostart_service.disable()

    def _on_cancel_clicked(self) -> None:
        self.reload_from_config()

    def _on_test_send_clicked(self) -> None:
        if self._notification_service is None:
            return
        try:
            self._notification_service.send_test_mail()
            QMessageBox.information(self, "テスト送信", "テストメールを送信しました。")
        except AppError as exc:
            show_error_dialog(self, exc)

    def _build_general_tab(self) -> QWidget:
        tab = QWidget()
        layout = QFormLayout(tab)
        self.autostart_check = QCheckBox()
        self.autostart_check.setChecked(True)
        layout.addRow("OS起動時自動起動", self.autostart_check)
        self.tray_hint_check = QCheckBox()
        self.tray_hint_check.setChecked(True)
        layout.addRow("×ボタンでトレイに格納したときに案内を表示", self.tray_hint_check)
        return tab

    def _build_engine_tab(self) -> QWidget:
        """8-7. ClamAV エンジン（clamdの接続・OSのサービス、clamonaccの場所）"""
        tab = QWidget()
        layout = QFormLayout(tab)
        self.connection_type_combo = QComboBox()
        self.connection_type_combo.addItem("自動（Windows: TCP / Linux: UNIX ソケット）", "auto")
        self.connection_type_combo.addItem("TCP", "tcp")
        if not _IS_WINDOWS:  # WindowsのclamdはUNIXソケットで待ち受けられない
            self.connection_type_combo.addItem("UNIX ソケット", "unix")
        self.connection_type_combo.currentIndexChanged.connect(self._update_engine_fields)
        self.tcp_host_edit = QLineEdit()
        self.tcp_host_edit.setPlaceholderText(_DEFAULT_CLAMD.tcp_host)
        self.tcp_port_spin = QSpinBox()
        self.tcp_port_spin.setRange(1, 65535)
        self.tcp_port_spin.setValue(_DEFAULT_CLAMD.tcp_port)
        self.unix_socket_edit = QLineEdit()
        self.unix_socket_edit.setPlaceholderText(_DEFAULT_CLAMD.unix_socket_path)
        self.connection_test_button = QPushButton("接続テスト")
        self.connection_test_button.setToolTip("入力中の接続先へ PING を送ります（保存前に試せます）")
        self.connection_test_button.clicked.connect(self._on_connection_test_clicked)
        self.connection_test_label = QLabel("")
        self.connection_test_label.setWordWrap(True)
        test_layout = QHBoxLayout()
        test_layout.addWidget(self.connection_test_button)
        test_layout.addWidget(self.connection_test_label, 1)
        connection_note = QLabel(
            "接続方式・ホスト・ポート・UNIX ソケットは、clamd.conf の TCPSocket / TCPAddr / LocalSocket と"
            "同じ値にしてください。保存した接続の設定は、アプリの再起動後に反映されます。"
        )
        connection_note.setWordWrap(True)
        self.service_name_edit = QLineEdit()
        self.service_name_edit.setPlaceholderText("空欄なら OS の既定（Windows: clamd / Linux: clamav-daemon）")
        self.clamd_auto_start_check = QCheckBox()
        self.clamonacc_path_edit = QLineEdit()
        self.clamonacc_path_edit.setPlaceholderText("clamonacc（PATHから探す）")
        clamonacc_note = QLabel(
            "clamonacc の場所は、次にリアルタイム監視を開始したときから使います"
            "（アプリの起動時に clamonacc が見つからず watchdog 方式になっている場合は、アプリの再起動が必要です）。"
        )
        clamonacc_note.setWordWrap(True)

        layout.addRow("接続方式", self.connection_type_combo)
        layout.addRow("ホスト", self.tcp_host_edit)
        layout.addRow("ポート", self.tcp_port_spin)
        layout.addRow("UNIX ソケット", self.unix_socket_edit)
        layout.addRow("", wrap_layout(test_layout))
        layout.addRow("", connection_note)
        layout.addRow("OS のサービス名", self.service_name_edit)
        layout.addRow("停止を検知したら自動で起動", self.clamd_auto_start_check)
        layout.addRow("clamonacc の場所", self.clamonacc_path_edit)
        layout.addRow("", clamonacc_note)
        if _IS_WINDOWS:  # UNIXソケット・clamonaccはLinux専用
            for widget in (self.unix_socket_edit, self.clamonacc_path_edit, clamonacc_note):
                layout.setRowVisible(widget, False)
        return tab

    def _update_engine_fields(self) -> None:
        """選んだ接続方式で使う欄だけを有効にする"""
        connection_type = self.connection_type_combo.currentData()
        if connection_type == "auto":
            connection_type = "tcp" if _IS_WINDOWS else "unix"
        self.tcp_host_edit.setEnabled(connection_type == "tcp")
        self.tcp_port_spin.setEnabled(connection_type == "tcp")
        self.unix_socket_edit.setEnabled(connection_type == "unix")

    def _entered_clamd_config(self, base: ClamdConfig) -> ClamdConfig:
        """入力中の接続の設定（空欄は既定値）。base（現在の設定）のタイムアウト等はそのまま使う"""
        return dataclasses.replace(
            base,
            connection_type=self.connection_type_combo.currentData(),
            tcp_host=self.tcp_host_edit.text().strip() or _DEFAULT_CLAMD.tcp_host,
            tcp_port=self.tcp_port_spin.value(),
            unix_socket_path=self.unix_socket_edit.text().strip() or _DEFAULT_CLAMD.unix_socket_path,
        )

    def _on_connection_test_clicked(self) -> None:
        if self._connection_tester is None or self._config_service is None:
            return
        clamd_config = self._entered_clamd_config(self._config_service.current.clamd)
        tester = self._connection_tester
        if self._connection_task.run("connection_test", lambda: tester(clamd_config)):
            self.connection_test_button.setEnabled(False)
            self.connection_test_label.setText("接続しています...")
            self.connection_test_label.setStyleSheet("")

    def _on_connection_test_finished(self, _name: str, result, error) -> None:
        self.connection_test_button.setEnabled(True)
        if error is None:
            self.connection_test_label.setText(f"接続できました: {result}")
            self.connection_test_label.setStyleSheet(f"color: {COLOR_OK};")
        else:
            detail = getattr(error, "detail", "") or str(error)
            self.connection_test_label.setText(f"接続できません: {detail}")
            self.connection_test_label.setStyleSheet(f"color: {COLOR_NG};")

    def _build_smtp_tab(self) -> QWidget:
        tab = QWidget()
        layout = QFormLayout(tab)
        self.smtp_host_edit = QLineEdit()
        self.smtp_port_spin = QSpinBox()
        self.smtp_port_spin.setRange(1, 65535)
        self.smtp_port_spin.setValue(587)
        self.smtp_user_edit = QLineEdit()
        self.smtp_password_edit = QLineEdit()
        self.smtp_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.smtp_from_edit = QLineEdit()
        self.smtp_to_edit = QLineEdit()
        self.smtp_encryption_combo = QComboBox()
        self.smtp_encryption_combo.addItems(["none", "ssl", "starttls"])
        self.smtp_encryption_combo.setCurrentText("starttls")
        self.smtp_test_button = QPushButton("テスト送信")

        layout.addRow("ホスト", self.smtp_host_edit)
        layout.addRow("ポート", self.smtp_port_spin)
        layout.addRow("ユーザー名", self.smtp_user_edit)
        layout.addRow("パスワード", self.smtp_password_edit)
        layout.addRow("送信元アドレス", self.smtp_from_edit)
        layout.addRow("宛先アドレス", self.smtp_to_edit)
        layout.addRow("暗号化方式", self.smtp_encryption_combo)
        self.smtp_encryption_note = QLabel(
            "none（暗号化なし）では、パスワードを平文で送らないよう認証を行いません。"
            "認証が必要なサーバーでは ssl か starttls を選んでください。"
        )
        self.smtp_encryption_note.setWordWrap(True)
        layout.addRow("", self.smtp_encryption_note)
        layout.addRow(self.smtp_test_button)
        return tab

    def _build_notification_tab(self) -> QWidget:
        tab = QWidget()
        layout = QFormLayout(tab)
        self.on_detect_mail_check = QCheckBox()
        self.on_detect_mail_check.setChecked(True)
        self.on_detect_desktop_check = QCheckBox()
        self.on_detect_desktop_check.setChecked(True)
        self.on_update_fail_check = QCheckBox()
        self.on_update_fail_check.setChecked(True)
        self.on_job_fail_check = QCheckBox()
        self.on_job_fail_check.setChecked(True)
        layout.addRow("検知時にメール通知", self.on_detect_mail_check)
        layout.addRow("検知時にデスクトップ通知", self.on_detect_desktop_check)
        layout.addRow("定義更新失敗時に通知", self.on_update_fail_check)
        layout.addRow("スケジュールジョブ失敗時に通知", self.on_job_fail_check)
        return tab

    def _build_realtime_tab(self) -> QWidget:
        tab = QWidget()
        layout = QFormLayout(tab)
        self.realtime_default_enabled_check = QCheckBox()
        self.realtime_default_action_combo = QComboBox()
        self.realtime_default_action_combo.addItem("自動隔離", "quarantine")
        self.realtime_default_action_combo.addItem("通知のみ（手動対応）", "notify_only")
        layout.addRow("起動時にリアルタイム監視を開始", self.realtime_default_enabled_check)
        layout.addRow("検知時アクションの既定値", self.realtime_default_action_combo)
        return tab

    def _build_update_tab(self) -> QWidget:
        tab = QWidget()
        layout = QFormLayout(tab)
        self.auto_update_check = QCheckBox()
        self.auto_update_check.setChecked(True)
        self.update_interval_spin = QSpinBox()
        self.update_interval_spin.setRange(1, 24)
        self.update_interval_spin.setValue(4)
        layout.addRow("自動更新の有効化", self.auto_update_check)
        layout.addRow("実行間隔（時間）", self.update_interval_spin)
        # freshclamの場所（コードレビューで追加）。既定の「freshclam」はPATHから探す。PATHに無い場合はフルパスを指定する
        self.freshclam_path_edit = QLineEdit()
        self.freshclam_path_edit.setPlaceholderText("freshclam（PATHから探す）")
        self.freshclam_path_edit.setToolTip(
            "freshclam の実行ファイル名またはフルパス。空欄で保存すると「freshclam」（PATHから探す）に戻します"
        )
        self.freshclam_browse_button = QPushButton("参照...")
        self.freshclam_browse_button.clicked.connect(self._on_browse_freshclam)
        path_layout = QHBoxLayout()
        path_layout.addWidget(self.freshclam_path_edit, 1)
        path_layout.addWidget(self.freshclam_browse_button)
        layout.addRow("freshclam の場所", wrap_layout(path_layout))
        return tab

    def _on_browse_freshclam(self) -> None:
        name_filter = "実行ファイル (*.exe)" if sys.platform == "win32" else ""
        path, _ = QFileDialog.getOpenFileName(self, "freshclam の場所", "", name_filter)
        if path:
            self.freshclam_path_edit.setText(QDir.toNativeSeparators(path))

    def _build_logging_tab(self) -> QWidget:
        tab = QWidget()
        layout = QFormLayout(tab)
        self.retention_days_combo = QComboBox()
        self.retention_days_combo.addItems(["30", "60", "90", "180", "365"])
        self.retention_days_combo.setCurrentText("90")
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.log_level_combo.setCurrentText("INFO")
        layout.addRow("保持期間（日）", self.retention_days_combo)
        layout.addRow("ログレベル", self.log_level_combo)
        return tab
