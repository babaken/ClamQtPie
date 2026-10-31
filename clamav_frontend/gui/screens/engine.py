"""
ClamAV エンジン サービス画面

画面レイアウト刷新で追加。従来ホーム画面にあったclamdの「起動」ボタンを独立した画面にし、
停止・定義データベースの再読み込み（RELOAD）・状態確認（PING）を加えた。
- 起動はapp.pyのClamdStarter（停止検知時の自動起動と共通）に委譲する（on_start_clamd）
- 停止・再読み込み・状態確認はBackgroundTaskでバックグラウンド実行する（sc / systemctl・pkexecの認証待ちで
  GUIを止めないため）
- 稼働状態の表示はHomeScreen.refresh_status()の結果（AppStatus）をapp.pyから受け取る（apply_status）
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout

from core.background_task import BackgroundTask
from core.exceptions import AppError, ClamdConnectionError
from core.logger import CATEGORY_SYSTEM, log_event
from gui import icons
from gui.error_dialog import show_confirm_dialog, show_error_dialog
from gui.screens.base import BaseScreen, wrap_layout
from gui.theme import COLOR_NG, COLOR_OK, COLOR_TEXT_MUTED, set_accent, set_variant
from gui.plain_text import plain

class EngineScreen(BaseScreen):
    screen_title = "ClamAV エンジン サービス (clamd)"
    screen_subtitle = "ウイルス検査を行う常駐サービス clamd の状態確認と、起動・停止・定義の再読み込みを行います。"

    def build_content(self) -> None:
        # 1. 状態カード
        self.status_card = self.begin_card()
        row = QHBoxLayout()
        icon_label = QLabel()
        icon_label.setPixmap(icons.pixmap("engine", 44))
        row.addWidget(icon_label)
        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        self.state_label = QLabel("確認中...")
        self.state_label.setObjectName("heroTitle")
        self.state_label.setWordWrap(True)
        self.endpoint_label = plain(QLabel("接続先: -"))
        self.engine_label = plain(QLabel("エンジンのバージョン: -"))
        self.database_label = plain(QLabel("定義データベース: -"))
        self.service_label = plain(QLabel("OSのサービス名: -"))
        text_box.addWidget(self.state_label)
        for label in (self.endpoint_label, self.engine_label, self.database_label, self.service_label):
            label.setObjectName("cardText")
            text_box.addWidget(label)
        row.addLayout(text_box, stretch=1)
        self.add_layout_widget(wrap_layout(row))

        # 2. 操作カード
        self.begin_card("サービスの操作")
        buttons = QHBoxLayout()
        self.start_button = set_variant(QPushButton("▶ サービスを開始"), "primary")
        self.stop_button = QPushButton("■ サービスを停止")
        self.reload_button = QPushButton("定義データベースを再読み込み")
        self.ping_button = QPushButton("状態を確認 (PING)")
        self.start_button.clicked.connect(self._on_start_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.reload_button.clicked.connect(self._on_reload_clicked)
        self.ping_button.clicked.connect(self._on_ping_clicked)
        for button in (self.start_button, self.stop_button, self.reload_button, self.ping_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.add_layout_widget(wrap_layout(buttons))
        hint = QLabel(
            "停止中はオンデマンド スキャン・スケジュール スキャン・リアルタイム監視の検査ができません。"
            "設定で自動起動が有効な場合でも、この画面で停止したあとは自動では起動しません。"
        )
        hint.setObjectName("cardText")
        hint.setWordWrap(True)
        self.add_layout_widget(hint)

        # 3. 出力カード
        self.begin_card("サービスの出力")
        self.output_area = QTextEdit()
        self.output_area.setReadOnly(True)
        self.output_area.setPlaceholderText("サービスの出力はありません。操作の結果がここに表示されます。")
        self.add_layout_widget(self.output_area)

        self._clamav_service = None
        self._controller = None
        self._on_start_clamd = None
        self._on_stop_requested = None
        self._on_stop_failed = None
        self._on_refresh = None
        self._realtime_manager = None
        self._running: bool | None = None
        self._starting = False
        self._task = BackgroundTask()
        self._task.finished.connect(self._on_task_finished)
        self._update_buttons()

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(
        self,
        clamav_service,
        controller,
        on_start_clamd=None,
        on_stop_requested=None,
        on_stop_failed=None,
        on_refresh=None,
        realtime_manager=None,
    ) -> None:
        """
        controller: ClamdServiceController（停止とサービス名の表示に使う）
        on_start_clamd: 起動の実処理（app.pyのClamdStarter）。起動試行を始めたらTrue、既に試行中ならFalseを返す。
            完了はon_start_clamd_finished()で受け取る
        on_stop_requested: この画面から停止を要求したとき、停止処理を始める前に呼ぶ（app.pyで停止検知時の自動起動を抑止する）。
            停止の完了後ではなく要求の時点で呼ぶのは、停止処理中（systemctl stopの待ち・sc stopのSTOP_PENDING）に
            定期確認のPINGが失敗して自動起動が走らないようにするため（レビューで指摘）
        on_stop_failed: 停止に失敗したとき（または停止処理を始められなかったとき）に呼ぶ（app.pyで抑止を元に戻す）
        on_refresh: 稼働状態の再取得（HomeScreen.refresh_status。結果はapply_status()で届く）
        """
        self._clamav_service = clamav_service
        self._controller = controller
        self._on_start_clamd = on_start_clamd
        self._on_stop_requested = on_stop_requested
        self._on_stop_failed = on_stop_failed
        self._on_refresh = on_refresh
        self._realtime_manager = realtime_manager
        self.endpoint_label.setText(f"接続先: {clamav_service.endpoint}")
        self.reload_service_name()
        self._update_buttons()

    def reload_service_name(self) -> None:
        """OSのサービス名の表示を更新する（設定の保存後にapp.pyから呼ばれる）"""
        if self._controller is not None:
            self.service_label.setText(f"OSのサービス名: {self._controller.service_name}")

    def apply_status(self, status) -> None:
        """HomeScreen.status_refreshed（AppStatus）を表示へ反映する"""
        self._running = status.clamd_running
        if status.clamd_running is None:
            self._set_state("確認中...", COLOR_TEXT_MUTED)
        elif status.clamd_running:
            self._set_state("稼働中: clamd はスキャンの準備ができています", COLOR_OK)
            self.engine_label.setText(f"エンジンのバージョン: {status.engine_version or '-'}")
            self.database_label.setText(f"定義データベース: {status.db_version} ({status.db_date})")
        else:
            self._set_state(f"停止しています: clamd（{self._endpoint()}）に接続できません", COLOR_NG)
            self.engine_label.setText("エンジンのバージョン: 該当なし")
            self.database_label.setText("定義データベース: 該当なし")
        set_accent(self.status_card, status.clamd_running is False)
        self._update_buttons()

    def _set_state(self, text: str, color: str) -> None:
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color: {color};")

    def _endpoint(self) -> str:
        return self._clamav_service.endpoint if self._clamav_service is not None else "-"

    def _update_buttons(self) -> None:
        busy = self._starting or self._task.is_running
        bound = self._clamav_service is not None
        self.start_button.setEnabled(bound and not busy and self._on_start_clamd is not None and self._running is not True)
        self.stop_button.setEnabled(bound and not busy and self._running is not False)
        self.reload_button.setEnabled(bound and not busy and self._running is True)
        self.ping_button.setEnabled(bound and not busy)

    def append_output(self, message: str) -> None:
        self.output_area.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    # -- 起動 -----------------------------------------------------
    def _on_start_clicked(self) -> None:
        if self._on_start_clamd is None or self._starting:
            return
        if not self._on_start_clamd():
            # 停止検知時の自動起動などが既に試行中。完了するとon_start_clamd_finished()で結果が表示される
            self.append_output("clamd の起動処理が既に実行中です。完了までお待ちください")
            return
        self._starting = True
        self.start_button.setText("起動中...")
        self.append_output(f"サービス「{self._controller.service_name}」の起動を要求しました")
        self._update_buttons()

    def on_start_clamd_finished(self, ok: bool, error=None) -> None:
        """clamd起動試行の完了時にapp.pyから呼ばれる（自動起動の完了でも呼ばれる）"""
        if self._starting or ok:
            self.append_output("clamd が起動しました" if ok else f"起動できませんでした: {_describe(error)}")
        self._starting = False
        self.start_button.setText("▶ サービスを開始")
        self._update_buttons()
        self._refresh()

    # -- 停止・再読み込み・状態確認 ---------------------------------------
    def _on_stop_clicked(self) -> None:
        message = "clamd を停止しますか？停止中はスキャンとリアルタイム監視の検査ができません。"
        if self._realtime_manager is not None and self._realtime_manager.is_running:
            message += "\n（リアルタイム監視は実行中のままですが、検査は失敗します）"
        if not show_confirm_dialog(self, "サービス停止の確認", message):
            return
        if self._on_stop_requested is not None:
            self._on_stop_requested()
        self.append_output(f"サービス「{self._controller.service_name}」の停止を要求しました")
        if not self._run_task("stop", self._controller.stop):
            # 別の操作が実行中（その間はボタンを無効にしているため通常は起きない）
            self.append_output("別の操作を実行中のため、停止できませんでした")
            if self._on_stop_failed is not None:
                self._on_stop_failed()

    def _on_reload_clicked(self) -> None:
        self.append_output("定義データベースの再読み込み（RELOAD）を指示します")
        self._run_task("reload", self._clamav_service.update_database)

    def _on_ping_clicked(self) -> None:
        self.append_output(f"{self._endpoint()} へ PING を送信します")
        self._run_task("ping", self._ping)

    def _ping(self) -> str:
        if not self._clamav_service.ping(retry=False):
            raise ClamdConnectionError(detail=f"{self._endpoint()} がPINGに応答しません")
        engine, db_version, db_date = self._clamav_service.get_version()
        return f"{engine} / 定義 {db_version} ({db_date})"

    def _run_task(self, name: str, func) -> bool:
        if not self._task.run(name, func):
            return False
        self._update_buttons()
        return True

    def _on_task_finished(self, name: str, result, error) -> None:
        self._update_buttons()
        if name == "stop":
            if error is None:
                log_event(CATEGORY_SYSTEM, "ClamAV エンジン サービス画面から clamd を停止しました")
                self.append_output("clamd を停止しました")
            else:
                self.append_output(f"停止できませんでした: {_describe(error)}")
                if self._on_stop_failed is not None:
                    self._on_stop_failed()
        elif name == "reload":
            if error is None and result:
                self.append_output("clamd が定義データベースの再読み込みを開始しました（RELOADING）")
            else:
                self.append_output(f"再読み込みを開始できませんでした: {_describe(error) if error else '予期しない応答'}")
        elif name == "ping":
            self.append_output(f"応答あり（PONG）: {result}" if error is None else f"応答なし: {_describe(error)}")
        if error is not None and name != "ping" and isinstance(error, AppError):
            show_error_dialog(self, error)
        self._refresh()

    def _refresh(self) -> None:
        if self._on_refresh is not None:
            self._on_refresh()


def _describe(error) -> str:
    if error is None:
        return "不明なエラー"
    if isinstance(error, AppError):
        return f"[{error.code}] {error.message}" + (f"（{error.detail}）" if error.detail else "")
    return str(error)
