"""
リアルタイム画面

参照: 画面設計書3章
Phase5: RealtimeManager / WatchTargetService / ExcludeService との結線を追加
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.exceptions import WatchTargetLostError
from gui.error_dialog import show_confirm_dialog, show_error_dialog
from gui.screens.base import BaseScreen, configure_table, wrap_layout

_ID_ROLE = 0x0102

#: 除外タブの表示ラベルとExcludeService上のtype値の対応(画面設計書3章)
_EXCLUDE_TYPES = [
    ("ファイル", "file"),
    ("フォルダ", "folder"),
    ("拡張子", "extension"),
    ("プロセス", "process"),
]


class RealtimeScreen(BaseScreen):
    screen_title = "リアルタイム ファイル保護"
    screen_subtitle = "監視対象フォルダー内でファイルが作成・変更されるとスキャンします。"

    def build_content(self) -> None:
        # 1. 監視状態トグル
        self.begin_card("リアルタイム フォルダー監視", "指定したフォルダーを監視し、作成・変更されたファイルをバックグラウンドでスキャンします。")
        toggle_layout = QHBoxLayout()
        self.toggle_button = QPushButton("監視を開始する")
        self.toggle_button.setCheckable(True)
        self.toggle_button.clicked.connect(self._on_toggle_clicked)
        self.mode_label = QLabel("方式: -")
        toggle_layout.addWidget(self.toggle_button)
        toggle_layout.addWidget(self.mode_label)
        self._add_hlayout(toggle_layout)

        # 2. 監視対象パス一覧
        self.begin_card("監視対象フォルダー")
        self.target_table = QTableWidget(0, 2)
        self.target_table.setHorizontalHeaderLabels(["パス", "有効"])
        self.add_layout_widget(self.target_table)
        target_action_layout = QHBoxLayout()
        self.add_target_button = QPushButton("監視対象を追加")
        self.remove_target_button = QPushButton("監視対象を削除")
        self.add_target_button.clicked.connect(self._on_add_target_clicked)
        self.remove_target_button.clicked.connect(self._on_remove_target_clicked)
        self.toggle_target_button = QPushButton("監視対象の有効/無効を切替")
        self.toggle_target_button.clicked.connect(self._on_toggle_target_clicked)
        target_action_layout.addWidget(self.add_target_button)
        target_action_layout.addWidget(self.remove_target_button)
        target_action_layout.addWidget(self.toggle_target_button)
        self._add_hlayout(target_action_layout)
        self.no_target_hint_label = QLabel(
            "監視対象が設定されていません。「監視対象を追加」から監視対象フォルダを設定してください。"
        )
        self.no_target_hint_label.setObjectName("cardText")
        self.add_layout_widget(self.no_target_hint_label)

        # 3. 除外設定タブ
        self.begin_card("除外")
        self.exclude_tabs = QTabWidget()
        self._exclude_tables: dict[str, QTableWidget] = {}
        for label, type_key in _EXCLUDE_TYPES:
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            table = QTableWidget(0, 2)
            table.setHorizontalHeaderLabels(["値", "有効"])
            configure_table(table)
            tab_layout.addWidget(table)
            self.exclude_tabs.addTab(tab, label)
            self._exclude_tables[type_key] = table
        self.add_layout_widget(self.exclude_tabs)

        # 画面設計書3章: 各区分ごとに追加（入力 or 参照）／削除／有効-無効トグル
        exclude_action_layout = QHBoxLayout()
        self.add_exclude_button = QPushButton("除外を追加")
        self.remove_exclude_button = QPushButton("除外を削除")
        self.toggle_exclude_button = QPushButton("除外の有効/無効を切替")
        self.add_exclude_button.clicked.connect(self._on_add_exclude_clicked)
        self.remove_exclude_button.clicked.connect(self._on_remove_exclude_clicked)
        self.toggle_exclude_button.clicked.connect(self._on_toggle_exclude_clicked)
        for btn in (self.add_exclude_button, self.remove_exclude_button, self.toggle_exclude_button):
            exclude_action_layout.addWidget(btn)
        self._add_hlayout(exclude_action_layout)

        # 4. 検知時アクション設定
        self.begin_card("検知時アクション")
        self.action_quarantine_radio = QRadioButton("自動隔離")
        self.action_notify_only_radio = QRadioButton("通知のみ（手動対応）")
        self.action_quarantine_radio.setChecked(True)
        self.action_quarantine_radio.toggled.connect(self._on_action_changed)
        self.add_layout_widget(self.action_quarantine_radio)
        self.add_layout_widget(self.action_notify_only_radio)

        self._realtime_manager = None
        self._watch_target_service = None
        self._exclude_service = None
        self._first_show_guided = False
        self._showing_scan_error = False

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(self, realtime_manager, watch_target_service, exclude_service) -> None:
        self._realtime_manager = realtime_manager
        self._watch_target_service = watch_target_service
        self._exclude_service = exclude_service

        self._realtime_manager.detected.connect(self._on_detected)
        self._realtime_manager.watch_target_lost.connect(self._on_watch_target_lost)
        self._realtime_manager.scan_error.connect(self._on_scan_error)
        self.mode_label.setText(f"方式: {getattr(self._realtime_manager, 'mode_name', '-')}")

        self.sync_action_from_config()
        self.reload_targets()
        self.reload_excludes()

    def sync_action_from_config(self) -> None:
        """検知時アクションのラジオボタンを設定値に合わせる（設定画面で既定値を保存した直後にも呼ばれる）"""
        if self._realtime_manager is None:
            return
        action = self._realtime_manager.config.default_action
        self.action_quarantine_radio.setChecked(action == "quarantine")
        self.action_notify_only_radio.setChecked(action == "notify_only")

    # -- 監視対象パス -----------------------------------------------------
    def reload_targets(self) -> None:
        if self._watch_target_service is None:
            return
        records = self._watch_target_service.list_all()
        self.target_table.setRowCount(0)
        for record in records:
            row = self.target_table.rowCount()
            self.target_table.insertRow(row)
            path_item = QTableWidgetItem(record.path)
            path_item.setData(_ID_ROLE, record.id)
            self.target_table.setItem(row, 0, path_item)
            self.target_table.setItem(row, 1, QTableWidgetItem("有効" if record.enabled else "無効"))
        self.no_target_hint_label.setVisible(len(records) == 0)
        # Windowsリアルタイム監視詳細設計書3.1節: 対象0件の間はON操作自体を無効化し、ツールチップで理由を表示
        has_enabled = any(r.enabled for r in records)
        running = self._realtime_manager is not None and self._realtime_manager.is_running
        self.toggle_button.setEnabled(has_enabled or running)
        self.toggle_button.setToolTip(
            "" if has_enabled or running else "監視対象フォルダを追加（有効化）すると開始できます"
        )

    def sync_toggle_state(self, running: bool) -> None:
        """ホーム画面・トレイから監視を開始/停止した場合も表示を合わせる（app.pyの定期更新から呼ばれる）"""
        if self.toggle_button.isChecked() != running:
            self.toggle_button.setChecked(running)
            self.toggle_button.setText("監視を停止する" if running else "監視を開始する")
            self.reload_targets()

    def showEvent(self, event) -> None:  # noqa: N802 - Qtのオーバーライド
        super().showEvent(event)
        # Windowsリアルタイム監視詳細設計書3.1節: 監視対象が未設定なら初回表示時に案内する（起動ごとに1回）
        if (
            not self._first_show_guided
            and self._watch_target_service is not None
            and not self._watch_target_service.list_all()
        ):
            self._first_show_guided = True
            if _ask_add_target_now(self):
                self._on_add_target_clicked()

    def _on_toggle_target_clicked(self) -> None:
        if self._watch_target_service is None:
            return
        rows = {idx.row() for idx in self.target_table.selectedIndexes()}
        if not rows:
            return
        item = self.target_table.item(next(iter(rows)), 0)
        target_id, path = item.data(_ID_ROLE), item.text()
        record = next((r for r in self._watch_target_service.list_all() if r.id == target_id), None)
        if record is None:
            return
        enable = not record.enabled
        self._watch_target_service.set_enabled(target_id, enable)
        self.reload_targets()
        # 稼働中は差分だけ反映する（watchdog: 該当パスのみ / clamonacc: 再起動）
        manager = self._realtime_manager
        if manager is not None and manager.is_running:
            if enable:
                manager.add_target(path)
            else:
                manager.remove_target(path)

    def _on_add_target_clicked(self) -> None:
        if self._watch_target_service is None:
            return
        path = QFileDialog.getExistingDirectory(self, "監視対象フォルダを選択")
        if not path:
            return
        self._watch_target_service.add(path)
        self.reload_targets()
        if self._realtime_manager is not None and self._realtime_manager.is_running:
            self._realtime_manager.add_target(path)

    def _on_remove_target_clicked(self) -> None:
        if self._watch_target_service is None:
            return
        selected_rows = {idx.row() for idx in self.target_table.selectedIndexes()}
        if not selected_rows:
            return
        row = next(iter(selected_rows))
        item = self.target_table.item(row, 0)
        target_id = item.data(_ID_ROLE)
        path = item.text()

        self._watch_target_service.remove(target_id)
        if self._realtime_manager is not None and self._realtime_manager.is_running:
            self._realtime_manager.remove_target(path)
        self.reload_targets()

    # -- 除外設定 -----------------------------------------------------
    def reload_excludes(self) -> None:
        if self._exclude_service is None:
            return
        all_rules = self._exclude_service.list_all()
        for type_key, table in self._exclude_tables.items():
            rules = [r for r in all_rules if r.type == type_key]
            table.setRowCount(0)
            for rule in rules:
                row = table.rowCount()
                table.insertRow(row)
                value_item = QTableWidgetItem(rule.value)
                value_item.setData(_ID_ROLE, rule.id)
                table.setItem(row, 0, value_item)
                table.setItem(row, 1, QTableWidgetItem("有効" if rule.enabled else "無効"))

    def _current_exclude_type(self) -> str:
        return _EXCLUDE_TYPES[self.exclude_tabs.currentIndex()][1]

    def _selected_exclude_id(self) -> int | None:
        table = self._exclude_tables[self._current_exclude_type()]
        rows = {idx.row() for idx in table.selectedIndexes()}
        if not rows:
            return None
        item = table.item(next(iter(rows)), 0)
        return item.data(_ID_ROLE) if item else None

    def _ask_exclude_value(self, type_key: str) -> str:
        """除外区分ごとの入力方法（ファイル/フォルダは参照ダイアログ、拡張子/プロセスはテキスト入力）"""
        if type_key == "file":
            path, _ = QFileDialog.getOpenFileName(self, "除外するファイルを選択")
            return path
        if type_key == "folder":
            return QFileDialog.getExistingDirectory(self, "除外するフォルダを選択")
        if type_key == "extension":
            text, ok = QInputDialog.getText(self, "拡張子の除外", "除外する拡張子（例: .log）")
            if not ok or not text.strip():
                return ""
            ext = text.strip().lower()
            return ext if ext.startswith(".") else f".{ext}"
        text, ok = QInputDialog.getText(self, "プロセスの除外", "除外するプロセス名（例: notepad.exe）")
        return text.strip() if ok else ""

    def _on_add_exclude_clicked(self) -> None:
        if self._exclude_service is None:
            return
        type_key = self._current_exclude_type()
        value = self._ask_exclude_value(type_key)
        if not value:
            return
        existing = {(r.type, r.value) for r in self._exclude_service.list_all()}
        if (type_key, value) not in existing:  # 同じ値の重複登録はしない
            self._exclude_service.add(type_key, value)
        self._after_exclude_changed(type_key)

    def _on_remove_exclude_clicked(self) -> None:
        if self._exclude_service is None:
            return
        exclude_id = self._selected_exclude_id()
        if exclude_id is None:
            return
        if not show_confirm_dialog(self, "除外設定の削除", "選択した除外設定を削除しますか？"):
            return
        self._exclude_service.remove(exclude_id)
        self._after_exclude_changed(self._current_exclude_type())

    def _on_toggle_exclude_clicked(self) -> None:
        if self._exclude_service is None:
            return
        exclude_id = self._selected_exclude_id()
        if exclude_id is None:
            return
        rule = next((r for r in self._exclude_service.list_all() if r.id == exclude_id), None)
        if rule is None:
            return
        self._exclude_service.set_enabled(exclude_id, not rule.enabled)
        self._after_exclude_changed(rule.type)

    def _after_exclude_changed(self, type_key: str) -> None:
        self.reload_excludes()
        # 画面設計書3章「除外リスト編集はON/OFFに関わらず可能（保存時に反映）」。
        # watchdog方式はイベントごとに照合するため即時反映、clamonacc方式はフォルダ除外の反映に再起動が必要
        manager = self._realtime_manager
        if manager is not None and manager.is_running and hasattr(manager, "on_excludes_changed"):
            manager.on_excludes_changed(type_key)

    # -- 監視トグル -----------------------------------------------------
    def _on_toggle_clicked(self, checked: bool) -> None:
        if self._realtime_manager is None:
            self.toggle_button.setChecked(False)
            return

        if checked:
            started = self._realtime_manager.start()
            if not started:
                self.toggle_button.setChecked(False)
                # clamonacc方式では対象があっても起動失敗し得る（その場合はscan_errorで別途表示済み）
                if not self._watch_target_service.list_enabled_paths():
                    _show_no_target_warning(self)
                return
            self.toggle_button.setText("監視を停止する")
        else:
            if not show_confirm_dialog(self, "監視停止の確認", "リアルタイム監視を停止しますか？"):
                self.toggle_button.setChecked(True)
                return
            self._realtime_manager.stop()
            self.toggle_button.setText("監視を開始する")

    def _on_action_changed(self, checked: bool) -> None:
        if self._realtime_manager is None:
            return
        self._realtime_manager.config.default_action = (
            "quarantine" if checked else "notify_only"
        )

    # -- RealtimeManagerからのシグナル受信 -----------------------------------
    def _on_detected(self, path: str, virus_name: str) -> None:
        # Phase7で通知連携するまでは何もしない（検知〜隔離自体はRealtimeManager内で完結）
        pass

    def _on_watch_target_lost(self, path: str) -> None:
        # エラーコード一覧E2001: 該当パスの監視停止・無効化（マネージャ側で実施済み）＋GUI通知
        self.reload_targets()
        show_error_dialog(self, WatchTargetLostError(detail=path))

    def _on_scan_error(self, error) -> None:
        # clamonaccの異常終了等で監視自体が止まった場合はトグル表示を戻す
        if self.toggle_button.isChecked() and not self._realtime_manager.is_running:
            self.toggle_button.setChecked(False)
            self.toggle_button.setText("監視を開始する")
        # エラーダイアログを表示している間に届いたエラーは重ねて表示しない（ログには記録済み）。
        # ダイアログのイベントループ中に次のエラーが届き、ダイアログが入れ子で積み重なっていた（コードレビューで修正）
        if self._showing_scan_error:
            return
        self._showing_scan_error = True
        try:
            show_error_dialog(self, error)
        finally:
            self._showing_scan_error = False

    def _add_hlayout(self, layout: QHBoxLayout) -> None:
        self.add_layout_widget(wrap_layout(layout))


def _ask_add_target_now(parent) -> bool:
    """初回案内ダイアログ。「今すぐ追加」ならTrue、「後で設定する」ならFalse"""
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("リアルタイム監視")
    box.setText(
        "リアルタイム監視の対象フォルダが設定されていません。"
        "監視を開始するには対象フォルダを追加してください。"
    )
    add_button = box.addButton("今すぐ追加", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("後で設定する", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is add_button


def _show_no_target_warning(parent) -> None:
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.warning(
        parent,
        "監視を開始できません",
        "監視対象が設定されていません。「追加」から監視対象フォルダを設定してください。",
    )
